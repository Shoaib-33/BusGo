import hashlib
import json
import os
import re
from operator import itemgetter

from dotenv import load_dotenv
from langchain.prompts import PromptTemplate
from langchain_chroma import Chroma
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain_core.documents import Document
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnableLambda
from langchain_google_genai import ChatGoogleGenerativeAI

from .data_loader import all_chunks

load_dotenv()

# --- Gemini LLM config ---
GOOGLE_MODEL = os.environ.get("GOOGLE_MODEL", "gemini-2.0-flash")
_gemini_llm = None

DISTRICT_NAMES = sorted({
    str(chunk["metadata"].get("district", "")).strip()
    for chunk in all_chunks
    if chunk["metadata"].get("district")
} | {
    str(chunk["metadata"].get("from_district", "")).strip()
    for chunk in all_chunks
    if chunk["metadata"].get("from_district")
} | {
    str(chunk["metadata"].get("to_district", "")).strip()
    for chunk in all_chunks
    if chunk["metadata"].get("to_district")
}, key=len, reverse=True)

DISTRICT_BY_LOWER = {district.lower(): district for district in DISTRICT_NAMES}

# --- Embeddings ---
embedding_model = HuggingFaceEmbeddings(model_name="sentence-transformers/all-MiniLM-L6-v2")

# --- Vector DB ---
vectordb = Chroma(
    collection_name="bus_data",
    embedding_function=embedding_model,
    persist_directory=os.environ.get("BUSGO_VECTORSTORE_DIR", "vectorstore")
)


def clean_metadata(metadata):
    cleaned = {}
    for key, value in metadata.items():
        if isinstance(value, list):
            cleaned[key] = ", ".join(str(v) for v in value)
        else:
            cleaned[key] = value
    return cleaned


INDEX_SCHEMA_VERSION = "route_chunks_v1"


def get_chunks_signature():
    payload = json.dumps(all_chunks, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get_existing_manifest():
    stored = vectordb.get(include=["metadatas"])
    for metadata in stored.get("metadatas", []):
        if metadata and metadata.get("type") == "index_manifest":
            return metadata
    return None


def should_rebuild_vectorstore():
    manifest = get_existing_manifest()
    if not manifest:
        return True
    return (
        manifest.get("schema_version") != INDEX_SCHEMA_VERSION
        or manifest.get("signature") != get_chunks_signature()
    )


if should_rebuild_vectorstore():
    print("Adding chunks to vector DB...")
    existing_ids = vectordb.get()["ids"]
    if existing_ids:
        vectordb.delete(ids=existing_ids)

    for chunk in all_chunks:
        metadata = chunk["metadata"].copy()
        if "provider" in metadata and metadata["provider"]:
            metadata["provider"] = metadata["provider"].strip().lower()
        vectordb.add_texts([chunk["content"]], metadatas=[clean_metadata(metadata)])

    vectordb.add_texts(
        ["BusGo RAG index manifest."],
        metadatas=[{
            "type": "index_manifest",
            "schema_version": INDEX_SCHEMA_VERSION,
            "signature": get_chunks_signature(),
        }]
    )
    print(f"Added {len(all_chunks)} chunks.")
else:
    print(f"Vector database already contains {len(vectordb.get()['ids'])} documents.")


def get_llm():
    global _gemini_llm

    if _gemini_llm is not None:
        return _gemini_llm

    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GOOGLE_API_KEY is not set. Add it to your .env file before using the AI assistant."
        )

    _gemini_llm = ChatGoogleGenerativeAI(
        temperature=0.2,
        model=GOOGLE_MODEL,
        google_api_key=api_key,
    )
    return _gemini_llm


prompt_template = """You are a friendly and helpful bus service assistant for Bangladesh bus services.

CRITICAL INSTRUCTIONS - READ CAREFULLY:
1. If the user asks about a SPECIFIC bus provider (like Hanif, Ena, Desh Travel, etc.), ONLY use information from that provider's context.
2. NEVER mix contact information, policies, or details between different providers.
3. When answering about contact information, address, or policy, make absolutely sure you're looking at the correct provider's data.
4. If you're not certain which provider the information belongs to, say you don't know.
4a. If the user asks for a specific policy type such as refund, cancellation, privacy, or terms, answer only that specific policy type if it appears in the context. If that specific policy is not present, say you don't have that specific policy information and provide provider contact details from the context if available.
5. For route availability, a provider is available ONLY if the same provider's coverage districts include BOTH the source district and destination district.
6. Do not infer that a provider serves a route just because it covers one of the two districts.
7. Route ticket fares come from scheduled services for the exact route/provider/bus type. Dropping point prices are local destination data and are not the route ticket fare when scheduled service fares are available.
8. Never bring up Eid, holidays, holiday packages, or holiday booking suggestions in a normal route/time/fare answer. Mention those only if the user explicitly asks about Eid/holiday/special service, or if the backend context contains an active holiday_context for a selected travel date.
9. If the user asks for normal route availability, fare, AC/Non-AC service, or departure time, your final answer must end after giving the relevant service details. Do not add a holiday/Eid booking question.

GENERAL INSTRUCTIONS:
- Answer ONLY from the context provided below
- Be conversational, friendly, and concise
- Keep a professional customer-service tone. Never address the user as "bhai", "vai", "bro", or similar casual labels.
- Address the user politely as "Sir" or "Madam" where natural. In Banglish replies, use a professional style such as "Sir, ..." or "Madam, ..." rather than "bhai".
- When a dropping point is known from context, state it clearly as information, for example: "Sir, apnar dropping point Dhap." or "Sir, Dhap-e namte hobe." Do not ask awkward confirmation questions like "Dhap-e namte hobe, tai na?" unless the dropping point is genuinely uncertain or the user must choose among multiple options.
- Always mention prices in "Taka"
- Use bullet points for lists
- If information is missing, say: "I don't have that information. Please contact the bus service directly."
- Before answering route questions, verify the exact source district, destination district, eligible providers, scheduled bus types, departure times, and service fares from the context.
- Do not mention Eid, holiday packages, or holiday surcharges in normal route/time/fare answers unless the user explicitly asks about holidays/Eid or the provided context clearly says the selected travel date is inside a holiday window.
- Use the conversation history to understand follow-up questions. For example, if the user previously asked about Rangpur to Dhaka and now asks about Gabtoli, treat Gabtoli as a Dhaka dropping point for that same route.
- Do not ask again for details that are already clear from the conversation history and retrieved context.
- Treat normal ticket questions as information requests. Give route, provider, fare, policy, and contact answers first.
- Only move toward booking when the user clearly asks to book, reserve, buy, or confirm a ticket. Do not collect passenger name or phone number just because the user asks about a ticket price or cheap ticket.
- Do not ask "Do you want to book for Eid?" or similar holiday follow-up unless the user asked about Eid/holidays.
- If the user asks to book after discussing a route, use the conversation history to continue from that route.
- Understand Banglish and mixed Bangla-English written in Latin letters. Examples: "amar dhaka theke rangpur e jawar ticket lagbe" means the user needs a ticket from Dhaka to Rangpur; "bhara koto" means asking the fare.
- Reply in the user's style when possible: if the user writes Banglish, a short Banglish-friendly answer is okay, while keeping route names, provider names, and prices exact.

Conversation History:
{chat_history}

Context Information:
{context}

User Question: {question}

Helpful Answer:"""

PROMPT = PromptTemplate(
    template=prompt_template,
    input_variables=["chat_history", "context", "question"]
)


def detect_provider_from_query(query: str):
    query_lower = query.lower()
    providers = ["hanif", "ena", "desh travel", "green line", "soudia", "shyamoli"]
    for provider in providers:
        if provider in query_lower:
            return provider
    return None


def format_docs(docs):
    return "\n\n".join(doc.page_content for doc in docs)


def dedupe_docs(docs):
    seen = set()
    unique_docs = []

    for doc in docs:
        key = (doc.page_content, tuple(sorted(doc.metadata.items())))
        if key not in seen:
            seen.add(key)
            unique_docs.append(doc)

    return unique_docs


def get_provider_names_from_docs(docs):
    provider_names = set()

    for doc in docs:
        metadata_provider = doc.metadata.get("provider")
        if metadata_provider and "Status: available" in doc.page_content:
            provider_names.add(str(metadata_provider).strip().lower())

        for match in re.findall(r"Available providers:\s*([^\n]+)", doc.page_content):
            if match.strip().lower() == "none":
                continue
            for provider in match.split(","):
                provider = provider.strip().lower()
                if provider:
                    provider_names.add(provider)

    return provider_names


def get_policy_docs_for_providers(provider_names, limit_per_provider=3):
    policy_docs = []

    for provider in sorted(provider_names):
        docs = vectordb.similarity_search(
            f"{provider} contact policy address terms",
            k=limit_per_provider,
            filter={"$and": [{"type": "policy"}, {"provider": provider}]}
        )
        policy_docs.extend(docs)

    return policy_docs


def wants_holiday_context(text: str):
    text_lower = text.lower()
    markers = [
        "eid", "holiday", "holidays", "festival", "puja", "durga",
        "special service", "special_services", "surcharge", "chuti",
        "eider", "eid er", "eid-e", "eid e", "utsob"
    ]
    return any(marker in text_lower for marker in markers)


def remove_unrequested_holiday_text(answer: str, query: str):
    if wants_holiday_context(query):
        return answer

    holiday_markers = [
        "eid", "eider", "eid-er", "holiday", "holidays", "festival",
        "surcharge", "special package", "holiday package", "chuti"
    ]
    kept_lines = []

    for line in answer.splitlines():
        line_lower = line.lower()
        if any(marker in line_lower for marker in holiday_markers):
            continue
        kept_lines.append(line)

    cleaned = "\n".join(kept_lines).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned or answer


def filter_context_docs(docs, query: str):
    allow_holiday = wants_holiday_context(query)
    filtered_docs = []

    for doc in docs:
        doc_type = doc.metadata.get("type")
        if doc_type in {"special_services", "holiday_calendar"} and not allow_holiday:
            continue
        filtered_docs.append(doc)

    return filtered_docs


def detect_route_pair(text):
    text_lower = text.lower()

    for source_lower, source in DISTRICT_BY_LOWER.items():
        for destination_lower, destination in DISTRICT_BY_LOWER.items():
            if source_lower == destination_lower:
                continue

            patterns = [
                rf"\bfrom\s+{re.escape(source_lower)}\s+to\s+{re.escape(destination_lower)}\b",
                rf"\b{re.escape(source_lower)}\s+to\s+{re.escape(destination_lower)}\b",
                rf"\bbetween\s+{re.escape(source_lower)}\s+and\s+{re.escape(destination_lower)}\b",
            ]

            if any(re.search(pattern, text_lower) for pattern in patterns):
                return source, destination

    return None, None


def exact_route_docs(query):
    source, destination = detect_route_pair(query)
    if not source or not destination:
        return []

    route_filter = {
        "$and": [
            {"from_district": source},
            {"to_district": destination},
        ]
    }

    docs = vectordb.get(
        where=route_filter,
        include=["documents", "metadatas"]
    )

    exact_docs = []
    for content, metadata in zip(docs.get("documents", []), docs.get("metadatas", [])):
        metadata = metadata or {}
        if metadata.get("type") == "route_summary":
            exact_docs.append((0, content, metadata))
        elif metadata.get("type") == "route_provider" and metadata.get("available") == "true":
            exact_docs.append((1, content, metadata))
        elif metadata.get("type") == "route_provider":
            exact_docs.append((2, content, metadata))

    exact_docs.sort(key=lambda item: item[0])
    return [
        Document(page_content=content, metadata=metadata)
        for _, content, metadata in exact_docs
    ]


def retrieve_context_docs(retriever, query):
    route_docs = exact_route_docs(query) + filter_context_docs(retriever.invoke(query), query)
    provider_names = get_provider_names_from_docs(route_docs)
    policy_docs = get_policy_docs_for_providers(provider_names)
    return dedupe_docs(route_docs + policy_docs)


def format_chat_history(chat_history=None):
    if not chat_history:
        return "No previous conversation."

    lines = []
    for item in chat_history:
        if isinstance(item, dict):
            role = item.get("role", "message")
            message = item.get("message", "")
        else:
            role = "message"
            message = str(item)

        if message:
            lines.append(f"{role}: {message}")

    return "\n".join(lines) if lines else "No previous conversation."


def build_retrieval_query(question: str, chat_history_text: str):
    return (
        "Use this conversation to understand the user's current follow-up question.\n"
        f"Conversation:\n{chat_history_text}\n\n"
        f"Current user question: {question}"
    )


def rewrite_query_for_retrieval(question: str, chat_history_text: str):
    prompt = f"""
Rewrite the current bus-service question into clear English for vector search.

Rules:
- Preserve exact district names, provider names, dropping point names, dates, phone numbers, and prices.
- Translate Banglish / romanized Bangla into English.
- Include relevant conversation context if the user asks a follow-up.
- Return only one rewritten search query, no explanation.

Conversation:
{chat_history_text}

Current question:
{question}
"""
    try:
        response = get_llm().invoke(prompt)
        content = getattr(response, "content", str(response)).strip()
        return content or question
    except Exception:
        return question


def get_rag_chain(provider: str = None):
    if provider:
        retriever = vectordb.as_retriever(
            search_type="mmr",
            search_kwargs={
                "k": 12,
                "fetch_k": 40,
                "filter": {"provider": provider.strip().lower()}
            }
        )
    else:
        retriever = vectordb.as_retriever(
            search_type="mmr",
            search_kwargs={"k": 18, "fetch_k": 60}
        )

    chain = (
        {
            "context": (
                itemgetter("retrieval_query")
                | RunnableLambda(lambda query: retrieve_context_docs(retriever, query))
                | format_docs
            ),
            "chat_history": itemgetter("chat_history"),
            "question": itemgetter("question")
        }
        | PROMPT
        | get_llm()
        | StrOutputParser()
    )

    return chain, retriever


def get_answer(query: str, provider: str = None, chat_history=None):
    provider = provider or detect_provider_from_query(query)
    chain, _ = get_rag_chain(provider)
    chat_history_text = format_chat_history(chat_history)
    retrieval_question = rewrite_query_for_retrieval(query, chat_history_text)
    answer = chain.invoke({
        "question": query,
        "chat_history": chat_history_text,
        "retrieval_query": build_retrieval_query(retrieval_question, chat_history_text)
    })
    return remove_unrequested_holiday_text(answer, query)


def get_answer_with_sources(query: str, provider: str = None, chat_history=None):
    provider = provider or detect_provider_from_query(query)
    chain, retriever = get_rag_chain(provider)

    chat_history_text = format_chat_history(chat_history)
    retrieval_question = rewrite_query_for_retrieval(query, chat_history_text)
    retrieval_query = build_retrieval_query(retrieval_question, chat_history_text)

    docs = retrieve_context_docs(retriever, retrieval_query)
    answer = chain.invoke({
        "question": query,
        "chat_history": chat_history_text,
        "retrieval_query": retrieval_query
    })
    answer = remove_unrequested_holiday_text(answer, query)

    return {
        "answer": answer,
        "contexts": [doc.page_content for doc in docs],
        "source_documents": docs
    }
