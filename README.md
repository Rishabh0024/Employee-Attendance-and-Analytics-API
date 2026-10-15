# Employee Attendance & Analytics API

This project is a backend API for managing employee attendance and generating attendance reports.

It is built using Python, FastAPI and MongoDB.

## Features

- Create and list employees.
- Record employee punch-in and punch-out.
- Calculate late minutes, overtime and work hours.
- Allow managers to correct attendance records.
- Keep a history of manual corrections.
- Generate monthly employee reports.
- Generate department attendance reports.
- Show a late employee leaderboard.
- Show daily department attendance trends.
- Check MongoDB query execution details.

## Requirements

- Python 3.11 or newer
- MongoDB 6.0 or newer
- FastAPI
- PyMongo

## Setup

Create a virtual environment:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Install the packages:

```powershell
python -m pip install -r requirements.txt
```

Create a `.env` file in the project folder:

```env
MONGO_URI=mongodb+srv://USERNAME:PASSWORD@YOUR-CLUSTER.mongodb.net/?retryWrites=true&w=majority
MONGO_DB=hrone_candidate
```

Replace the example values with your MongoDB Atlas details.

Do not upload `.env` or database credentials to GitHub.

## Run the application

```powershell
uvicorn app.main:app --port 8000
```

## API Documentation

After starting the application, open:

- Health check: http://127.0.0.1:8000/health
- Swagger UI: http://127.0.0.1:8000/docs

The application creates the required MongoDB indexes when it starts.

All application code is kept in `app/main.py`.
