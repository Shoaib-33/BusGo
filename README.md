# BusGo - Banglish AI Bus Ticket Booking Application

BusGo is a Bangla/Banglish-friendly bus ticket booking system with an AI assistant, seat selection, demo payment, user dashboard, admin dashboard, refund workflow, RAG retrieval, and RAGAS evaluation.

The project is built with:

- FastAPI
- Jinja templates
- SQLite
- ChromaDB
- LangChain
- Gemini
- Sentence Transformers
- RAGAS

## Main Features

### Banglish AI Chatbot

The chatbot supports English, Bangla-style English, and Banglish queries such as:

```text
Dhaka theke Rangpur e shobcheye kom dam e kon AC bus ase?
amar ticket cancel krte hbe
refund pabo kivabe
Dhaka theke Coxbazar e kom dam e AC bus kon jay?
```

The assistant can:

- Answer route, fare, timing, provider, contact, and policy questions
- Reply naturally in Banglish when the user writes Banglish
- Identify cheapest AC/Non-AC buses
- Mention bus departure times in route answers
- Start and complete booking flows
- Ask for missing booking details
- Show seat picker when booking details are ready
- Hold selected seats until demo payment
- Cancel tickets
- Explain refund status after cancellation

### Booking Flow

The chatbot can collect:

- Passenger name
- Phone number
- Source district
- Destination district
- Bus provider
- Bus type
- Dropping point
- Travel date
- Passenger count
- Departure time
- Seat numbers

If the selected service has multiple departure times, the chatbot asks the user to choose a time before seat selection. The backend does not silently select the first time for multi-time services.

The selected departure time is shown during:

- Seat selection
- Payment-hold message
- Booking lookup
- Booking confirmation

## Docker Setup

Create a `.env` file in the project root before starting the container:

```env
GOOGLE_API_KEY=your_gemini_api_key
GOOGLE_MODEL=gemini-2.0-flash
```

Build and run with Docker Compose:

```bash
docker compose up --build
```

Open the app:

```text
http://127.0.0.1:8004
```

Useful commands:

```bash
docker compose up -d --build
docker compose logs -f app
docker compose down
```

Docker persists runtime data in named volumes:

- `busgo_data`: SQLite booking DB, demo payment DB, Chroma vectorstore
- `busgo_hf_cache`: Hugging Face embedding model cache

To reset all Docker runtime data:

```bash
docker compose down -v
```

### Seat Selection

Bookings are not saved as active tickets until the user selects seats.

The seat picker supports:

- Booked seat blocking
- Female-reserved seat indicators
- Window-seat indicators
- Selected seat validation
- Per-service seat locking by provider, route, date, bus type, and departure time

### Demo Payment

The app includes a demo wallet/payment system using SQLite.

Supported demo providers:

- bKash
- Nagad

Seeded demo PIN:

```text
1234
```

Payment flow:

1. Chatbot creates a payment-pending booking.
2. Seats are held for 5 minutes.
3. User pays through demo bKash/Nagad.
4. Ticket becomes active after successful payment.
5. PDF ticket download becomes available.

### Refund Workflow

Refund behavior:

- If a paid ticket is cancelled, a refund request is automatically created for admin approval.
- The chatbot tells the user:

```text
Apnar refund request admin panel e dewa hoyeche. Apni 48 hrs er moddhe refund peye jaben.
```

- If the ticket was not paid, the chatbot explains that no money was deducted and no refund is needed.
- After admin approval, the refund amount is returned to the same demo wallet used for payment.

### User Dashboard

Logged-in users can view:

- Active tickets
- Payment-pending tickets
- Travel history
- Cancelled tickets
- PDF download links
- Payment completion buttons
- Refund request/cancellation options

### Admin Dashboard

Admin users get a sidebar dashboard with:

- Dashboard summary
- Bookings
- Routes
- Buses and seats
- Users
- Operators
- Reports
- Refunds
- Settings

Admin can:

- View system-wide bookings
- View platform revenue
- View pending refunds
- Approve refund requests
- View users, operators, routes, and service summaries

Wallet balances are intentionally hidden from the admin dashboard.

### RAG Pipeline

The AI assistant retrieves data from:

- `data.json`
- `holiday_calendar.json`
- `attachment/*.txt`

The RAG pipeline creates:

- Route summary chunks
- Route-provider chunks
- Provider info chunks
- Policy/contact chunks
- Dropping point chunks

It stores embeddings in ChromaDB and sends retrieved context to Gemini.

The `/query/detailed` endpoint returns:

- `answer`
- `contexts`
- `source_documents`

This is used for RAGAS evaluation.

### Holiday Fare Rules

Holiday and Eid surcharge windows can be configured in:

```text
holiday_calendar.json
```

Example:

```json
{
  "holidays": [
    {
      "name": "Eid-ul-Fitr",
      "type": "eid",
      "start_date": "2026-03-20",
      "end_date": "2026-03-24",
      "surcharge_percent": 25,
      "note": "Book at least 7 days in advance."
    }
  ]
}
```

The surcharge is applied only if the selected travel date is inside the configured holiday window.

## Project Structure

```text
backend/
  main.py                 FastAPI routes, chatbot flow, booking/payment/refund logic
  database.py             Booking/user SQLite helpers
  payment_database.py     Demo wallet, payment, and refund SQLite helpers
  models.py               Pydantic models
  rag_pipeline.py         RAG pipeline using ChromaDB, LangChain, Gemini
  data_loader.py          Data and policy chunk generation

templates/
  base.html
  index.html
  assistant.html
  dashboard.html
  admin.html
  bookings.html
  login.html
  providers.html
  routes.html

static/
  css/style.css
  js/main.js

attachment/               Provider policy/contact text files
tests/
  golden_dataset.json
  golden_report.json
  ragas_banglish_dataset_10.json
  ragas_banglish_report.json
  ragas_banglish_fpr_report.csv
  ragas_banglish_fpr_report.json

data.json                 Routes, providers, fares, districts, dropping points
holiday_calendar.json     Holiday surcharge rules
bus_bookings.db           Booking/user SQLite database
demo_payments.db          Demo wallet/payment/refund SQLite database
run_golden_tests.py       Golden regression evaluator
run_ragas_eval.py         RAGAS evaluator
requirements.txt
Dockerfile
docker-compose.yml
```

## Setup

Create and activate a virtual environment:

```powershell
python -m venv venv
.\venv\Scripts\activate
```

Install dependencies:

```powershell
pip install -r requirements.txt
```

If you want to run RAGAS:

```powershell
.\venv\Scripts\python.exe -m pip install ragas datasets
```

Create a `.env` file in the project root:

```env
GOOGLE_API_KEY=your_google_api_key_here
GOOGLE_MODEL=gemini-2.0-flash
```

Get a Google AI Studio API key from:

```text
https://aistudio.google.com/app/apikey
```

`GOOGLE_MODEL` is optional.

## Run Locally

The current development port used in this project is `8004`.

Start the FastAPI app:

```powershell
.\venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8004
```

Open:

```text
http://127.0.0.1:8004
```

Useful pages:

```text
http://127.0.0.1:8004/                  Book Ticket
http://127.0.0.1:8004/assistant-page    AI Assistant
http://127.0.0.1:8004/bookings-page     My Bookings by phone
http://127.0.0.1:8004/dashboard-page    User Dashboard
http://127.0.0.1:8004/admin-page        Admin Dashboard
http://127.0.0.1:8004/providers-page    Providers
http://127.0.0.1:8004/routes-page       Routes & Fares
http://127.0.0.1:8004/login-page        Login
http://127.0.0.1:8004/docs              API docs
```

If port `8004` is already in use:

```powershell
$pid=(Get-NetTCPConnection -LocalPort 8004 -State Listen).OwningProcess
Stop-Process -Id $pid -Force
```

Then start the app again.

## Demo Accounts

Admin:

```text
admin@busgo.local
admin123
```

Demo wallet PIN:

```text
1234
```

## Docker

```powershell
docker compose up --build
```

Then open:

```text
http://127.0.0.1:8000
```

## Example Banglish Queries

```text
Dhaka theke Rangpur e shobcheye kom dam e kon AC bus ase?
Dhaka theke Rangpur e kom dam er Non AC bus konta?
Dhaka theke Rajshahi 500 takar niche kon kon bus ase?
Dhaka theke Coxbazar e kom dam e AC bus kon jay?
Hanif er contact number ar address dao
Ena Paribahan er privacy policy ki?
amar ticket cancel krte hbe
refund pabo kivabe
amar mobile number e booking ase kina check kore den
```

Example answer for:

```text
Dhaka theke rangpur e shobcheye kom dam e kon bus ase AC?
```

Expected answer:

```text
National Travels AC, 720 Taka, departure times 10:00 and 22:00.
```

## Golden Evaluation

The project includes a golden regression dataset:

```text
tests/golden_dataset.json
```

Start the app first:

```powershell
.\venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8004
```

Run all golden tests:

```powershell
.\venv\Scripts\python.exe run_golden_tests.py --base-url http://127.0.0.1:8004
```

Run one category:

```powershell
.\venv\Scripts\python.exe run_golden_tests.py --category route_fare
```

Run one case:

```powershell
.\venv\Scripts\python.exe run_golden_tests.py --case seat_ui_payload_shape
```

Report:

```text
tests/golden_report.json
```

## RAGAS Evaluation

RAGAS is used for chatbot/RAG answer evaluation.

For this project, the most useful metrics are:

- Faithfulness
- Context precision
- Context recall

RAGAS is not used to verify booking/payment/database correctness. Use golden/API tests for those flows.

### Banglish RAGAS Dataset

Dataset:

```text
tests/ragas_banglish_dataset_10.json
```

It contains 10 Banglish questions about:

- Cheapest buses
- AC/Non-AC fares
- Departure times
- Provider contact
- Provider policy

### Step 1: Collect Chatbot Answers

```powershell
.\venv\Scripts\python.exe run_ragas_eval.py --dataset tests\ragas_banglish_dataset_10.json --base-url http://127.0.0.1:8004 --limit 10 --collect-only --report-json tests\ragas_banglish_report.json
```

### Step 2: Run Faithfulness, Precision, Recall

```powershell
.\venv\Scripts\python.exe run_ragas_eval.py --from-report tests\ragas_banglish_report.json --limit 10 --metrics faithfulness,context_precision,context_recall --max-contexts 5 --report-json tests\ragas_banglish_fpr_report.json --report-csv tests\ragas_banglish_fpr_report.csv
```

Output:

```text
tests/ragas_banglish_fpr_report.json
tests/ragas_banglish_fpr_report.csv
```

### Current Banglish RAGAS Result

Latest report:

```text
tests/ragas_banglish_fpr_report.csv
```

Average scores:

```text
Faithfulness:       0.921875  (~92.19%)
Context Precision:  0.864583  (~86.46%)
Context Recall:     1.000000  (100%)
```

Rows evaluated:

```text
8
```

Two of the 10 cases were not included in the latest report, likely because Gemini quota/rate-limit issues interrupted answer collection or evaluation.

Interpretation:

- High faithfulness means most answers are grounded in retrieved context.
- Perfect context recall means the required information was retrieved.
- Lower context precision means retrieval still includes some unrelated chunks.

Recommended improvements:

- Filter route contexts more strictly by detected source and destination.
- Return fewer high-quality contexts to the LLM and RAGAS.
- For questions like "500 takar niche", list all matching services, not only one.
- For "cheapest" questions, answer the cheapest option first and avoid extra providers unless comparison is requested.

## Important API Endpoints

```text
POST /query                         Chatbot main endpoint
POST /query/detailed                RAG answer with contexts/source documents
POST /seat-selection/confirm        Confirm selected seats
GET  /seat-layout                   Seat layout for a service
POST /payments/demo                 Demo bKash/Nagad payment
POST /refunds/request/{booking_id}  Request refund
POST /refunds/approve/{refund_id}   Admin approve refund
GET  /user/dashboard                User dashboard data
GET  /admin/dashboard               Admin dashboard data
```

## Troubleshooting

### Port already in use

If you see:

```text
[Errno 10048] only one usage of each socket address is normally permitted
```

Run:

```powershell
$pid=(Get-NetTCPConnection -LocalPort 8004 -State Listen).OwningProcess
Stop-Process -Id $pid -Force
```

Then restart the app.

### Gemini quota exhausted

If Gemini requests fail:

- Make sure `.env` has a valid `GOOGLE_API_KEY`.
- Make sure your Google AI Studio project has quota.
- Wait for quota reset or use another API key/project.
- Restart the FastAPI server after changing `.env`.

### ChromaDB stale data

If retrieval seems stale:

- Restart the app.
- If needed, delete `vectorstore/` and start again.

### Missing dependencies

```powershell
pip install -r requirements.txt
```

For RAGAS:

```powershell
.\venv\Scripts\python.exe -m pip install ragas datasets
```

## License

MIT License. See `LICENSE`.
