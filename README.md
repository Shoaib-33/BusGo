# 🚌 Bus Ticket Booking Application

A comprehensive bus ticket booking system with AI-powered search capabilities, built using FastAPI and a RAG (Retrieval-Augmented Generation) pipeline for intelligent query handling.

---

## 🎯 Project Overview

This application allows users to:
- 🔍 Search for buses using natural language queries
- 🎫 Book tickets with basic information (name and phone number)
- 📋 View and manage their bookings
- 🚌 Access detailed bus provider information (routes, fares, policies, contact details)
- 💬 Ask questions about bus services and get AI-powered responses

---

## ✨ Key Features

- **RAG Pipeline Integration**: Uses LangChain with Google Generative AI for intelligent query processing
- **Vector Search**: ChromaDB with sentence transformers for semantic search capabilities
- **Natural Language Queries**: Ask questions like "Are there any buses from Dhaka to Rajshahi under 500 taka?"
- **SQLite Database**: Reliable booking data persistence
- **JSON-based Route Data**: Flexible data management for districts, routes, and providers
- **Complete CRUD Operations**: Create, read, update, and delete bookings
- **HTML/CSS/JS Frontend**: Served directly by FastAPI via static files and Jinja2 templates

---

## 📂 Project Structure

```
Bus-ticket-booking-application/
│
├── backend/
│   ├── main.py                 # FastAPI application entry point
│   ├── database.py             # SQLite database configuration
│   ├── models.py               # Pydantic models
│   ├── rag_pipeline.py         # RAG implementation
│   └── data_loader.py          # Data loading utilities
│
├── static/
│   ├── css/
│   │   └── style.css           # Application styles
│   └── js/
│       └── main.js             # Frontend JavaScript
│
├── templates/                  # Jinja2 HTML templates
│
├── data.json                   # Main data file (routes, districts, providers)
│
├── attachments/
│   ├── hanif.txt               # Hanif bus provider information
│   ├── ena.txt                 # Ena bus provider information
│   └── ...                     # Other bus provider files
│
├── bus_booking.db              # SQLite database (auto-generated)
├── requirements.txt            # Python dependencies
├── Dockerfile                  # Docker build instructions
├── docker-compose.yaml         # Docker Compose configuration
├── .env                        # Environment variables (create this)
└── README.md                   # This file
```

---

## 🚀 Option 1: Run with Docker Compose (Recommended)

### Prerequisites
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) installed and running

### 1️⃣ Clone the Repository

```bash
git clone https://github.com/yourusername/Bus-ticket-booking-application.git
cd Bus-ticket-booking-application
```

### 2️⃣ Create a `.env` File

Create a `.env` file in the root directory:

```env
GOOGLE_API_KEY=your_google_api_key_here
DATABASE_PATH=./bus_booking.db
CHROMA_PERSIST_DIRECTORY=./chroma_db
```

> **To get a Google API Key:** Visit [Google AI Studio](https://makersuite.google.com/app/apikey), create a new key, and paste it above.

### 3️⃣ Build and Run

```bash
docker compose up --build
```

### 4️⃣ Access the Application

| Service | URL |
|---|---|
| Web Frontend | http://localhost:8000 |
| FastAPI Swagger Docs | http://localhost:8000/docs |
| FastAPI Redoc | http://localhost:8000/redoc |

---

## 🛠️ Option 2: Run Locally (Without Docker)

### Prerequisites
- Python 3.10
- pip

### 1️⃣ Clone & Set Up Virtual Environment

```bash
git clone https://github.com/yourusername/Bus-ticket-booking-application.git
cd Bus-ticket-booking-application
python -m venv venv
```

**Activate — Windows:**
```bash
venv\Scripts\activate
```

**Activate — Mac/Linux:**
```bash
source venv/bin/activate
```

### 2️⃣ Install Dependencies

```bash
pip install -r requirements.txt
```

### 3️⃣ Configure Environment Variables

Create a `.env` file in the root directory:

```env
GOOGLE_API_KEY=your_google_api_key_here
DATABASE_PATH=./bus_booking.db
CHROMA_PERSIST_DIRECTORY=./chroma_db
```

### 4️⃣ Run the Application

```bash
uvicorn backend.main:app --reload --port 8000
```

Then open http://localhost:8000 in your browser.

---

## 🐳 Docker Details

### Overview

The image is based on `python:3.10-slim-bookworm` (Debian 12) which satisfies ChromaDB's sqlite3 >= 3.35 requirement. Key points:

- `numpy==1.26.4` and `torch==2.1.0+cpu` are installed first to prevent version conflicts
- The full LangChain stack is pinned to ensure compatibility
- `libgomp1` is included for PyTorch CPU threading support
- FastAPI serves both the API and the HTML/CSS/JS frontend on a single port `8000`

### Useful Docker Compose Commands

```bash
# Build and start
docker compose up --build

# Start in detached mode (background)
docker compose up -d --build

# View logs
docker compose logs -f

# Stop the application
docker compose down

# Stop and remove volumes
docker compose down -v

# Rebuild from scratch
docker compose build --no-cache
docker compose up
```

### Useful Docker Commands

```bash
# View running containers
docker ps

# View container logs
docker logs <container_name>

# Remove stopped containers
docker container prune

# Remove unused images
docker image prune
```

---

## 🤖 RAG Pipeline Architecture

1. **Data Ingestion** — Bus provider information and route data are loaded from `data.json` and `.txt` files in `attachments/`
2. **Embedding Generation** — Sentence transformers convert text into vector embeddings
3. **Vector Storage** — ChromaDB stores embeddings for fast semantic search
4. **Query Processing** — User questions are embedded and matched against stored vectors
5. **Context Retrieval** — Relevant information is retrieved from the vector database
6. **Response Generation** — Google Generative AI (Gemini) generates natural language responses based on retrieved context

---

## 📝 Example Queries

- "Are there any buses from Dhaka to Rajshahi under 500 taka?"
- "Show all bus providers operating from Chittagong to Sylhet."
- "What are the contact details of Hanif Bus?"
- "Can I cancel my booking for the bus from Dhaka to Barishal on 15th November?"
- "What is the privacy policy of Ena Paribahan?"
- "Which bus is cheapest from Dhaka to Cox's Bazar?"

---

## 🛠️ Troubleshooting

**Port already in use**
```bash
# Windows — find and kill the process
netstat -ano | findstr :8000
taskkill /PID <PID> /F
```

**Google API Key Error**
- Ensure your `.env` file contains a valid `GOOGLE_API_KEY`
- Check that the key has access to Generative AI services

**ChromaDB sqlite3 Error (local only)**
- Use Python 3.10+ and ensure your system sqlite3 is >= 3.35
- The Docker image handles this automatically via the `bookworm` base image

**ChromaDB Permission Error**
- Delete the `chroma_db` directory and restart the application

**Database Locked**
- Close any other processes accessing the SQLite database and restart the server

**Module Not Found**
- Ensure your virtual environment is activated
- Run `pip install -r requirements.txt` again

---

## 🎥 Demo Video

[https://youtu.be/wBabHF555jU](https://youtu.be/wBabHF555jU)

---

## 📄 License

This project is open source and available under the [MIT License](LICENSE).

---

**Built with:** FastAPI • HTML/CSS/JS • LangChain • Google Generative AI • ChromaDB • SQLite • Docker
