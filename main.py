import os
import certifi

# Force Python on Mac/VPN to use trusted internet certificates
os.environ["SSL_CERT_FILE"] = certifi.where()
os.environ["SSL_CERT_DIR"] = certifi.where()

from fastapi import FastAPI, HTTPException, UploadFile, File, Depends, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from passlib.context import CryptContext
from jose import JWTError, jwt
from datetime import datetime, timedelta
import sqlite3
from openai import OpenAI
import csv
import io
from typing import Optional
from dotenv import load_dotenv
import json

load_dotenv()

# Configure Gemini
# --- DYNAMIC AI ROUTER SETUP ---
with open("llm_config.json", "r") as f:
    llm_config = json.load(f)

active_ai = llm_config["active_provider"]
ai_settings = llm_config["providers"][active_ai]

ai_client = OpenAI(
    base_url=ai_settings["base_url"],
    api_key=os.environ.get(ai_settings["env_key"])
)
    
app = FastAPI(title="AI Finance App - Multi-Month")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- SECURITY CONFIGURATION ---
SECRET_KEY = "your-very-secret-key-change-this-later"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7 # Token lasts 7 days

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/login")

def verify_password(plain_password, hashed_password): return pwd_context.verify(plain_password, hashed_password)
def get_password_hash(password): return pwd_context.hash(password)

def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def get_current_user(token: str = Depends(oauth2_scheme)):
    credentials_exception = HTTPException(status_code=401, detail="Could not validate credentials")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None: raise credentials_exception
    except JWTError:
        raise credentials_exception
        
    conn = sqlite3.connect("finance.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE username = ?", (username,))
    user = cursor.fetchone()
    conn.close()
    
    if user is None: raise credentials_exception
    return user[0]

# --- DATABASE SETUP ---
def init_db():
    conn = sqlite3.connect("finance.db")
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, name TEXT NOT NULL, starting_balance REAL NOT NULL, UNIQUE(user_id, name))''')
    
    # Budgets & Transactions now include month_year!
    cursor.execute('''CREATE TABLE IF NOT EXISTS budgets (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, category TEXT NOT NULL, amount REAL NOT NULL, type TEXT DEFAULT 'expense', month_year TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS transactions (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, date TEXT, description TEXT, amount REAL, category TEXT, type TEXT DEFAULT 'debit', month_year TEXT)''')
    
    # Migrations for existing DBs
    try: cursor.execute("ALTER TABLE budgets ADD COLUMN month_year TEXT")
    except: pass
    try: cursor.execute("ALTER TABLE transactions ADD COLUMN month_year TEXT")
    except: pass

    conn.commit()
    conn.close()

init_db()

# --- AUTH ENDPOINTS ---
class UserCreate(BaseModel):
    username: str
    password: str

@app.post("/api/register")
def register(user: UserCreate):
    conn = sqlite3.connect("finance.db")
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)", (user.username, get_password_hash(user.password)))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="Username already exists")
    conn.close()
    return {"message": "User created successfully"}

@app.post("/api/login")
def login(form_data: OAuth2PasswordRequestForm = Depends()):
    conn = sqlite3.connect("finance.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, password_hash FROM users WHERE username = ?", (form_data.username,))
    user = cursor.fetchone()
    conn.close()
    if not user or not verify_password(form_data.password, user[1]):
        raise HTTPException(status_code=401, detail="Incorrect username or password")
    return {"access_token": create_access_token(data={"sub": form_data.username}), "token_type": "bearer"}

# --- MODELS ---
class AccountEntry(BaseModel): name: str; starting_balance: float
class BudgetEntry(BaseModel): category: str; amount: float; type: str = "expense"; month_year: str
class ClearDataRequest(BaseModel): transactions: bool; budgets: bool; accounts: bool; month_year: str
class AskAIRequest(BaseModel): question: str; month_year: str

# --- PROTECTED APP ENDPOINTS ---
@app.get("/")
def serve_homepage(): return FileResponse("index.html")

@app.post("/api/accounts")
def add_account(entry: AccountEntry, user_id: int = Depends(get_current_user)):
    conn = sqlite3.connect("finance.db")
    cursor = conn.cursor()
    cursor.execute('''INSERT INTO accounts (user_id, name, starting_balance) VALUES (?, ?, ?)
        ON CONFLICT(user_id, name) DO UPDATE SET starting_balance=excluded.starting_balance''', 
        (user_id, entry.name, entry.starting_balance))
    conn.commit()
    conn.close()
    return {"message": "Account updated!"}

@app.get("/api/accounts")
def get_accounts(user_id: int = Depends(get_current_user)):
    conn = sqlite3.connect("finance.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, starting_balance FROM accounts WHERE user_id = ?", (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return [{"id": row[0], "name": row[1], "balance": row[2]} for row in rows]

@app.post("/api/budgets")
def add_budget(entry: BudgetEntry, user_id: int = Depends(get_current_user)):
    conn = sqlite3.connect("finance.db")
    cursor = conn.cursor()
    # Check if budget exists for this month
    cursor.execute("SELECT id FROM budgets WHERE user_id=? AND category=? AND month_year=?", (user_id, entry.category, entry.month_year))
    if cursor.fetchone():
        cursor.execute("UPDATE budgets SET amount=?, type=? WHERE user_id=? AND category=? AND month_year=?", (entry.amount, entry.type, user_id, entry.category, entry.month_year))
    else:
        cursor.execute("INSERT INTO budgets (user_id, category, amount, type, month_year) VALUES (?, ?, ?, ?, ?)", (user_id, entry.category, entry.amount, entry.type, entry.month_year))
    conn.commit()
    conn.close()
    return {"message": "Budget added successfully!"}

@app.delete("/api/budgets/{budget_id}")
def delete_budget(budget_id: int, user_id: int = Depends(get_current_user)):
    conn = sqlite3.connect("finance.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM budgets WHERE id = ? AND user_id = ?", (budget_id, user_id))
    conn.commit()
    conn.close()
    return {"message": "Budget deleted!"}

@app.get("/api/summary")
def get_budget_summary(month_year: str, user_id: int = Depends(get_current_user)):
    conn = sqlite3.connect("finance.db")
    cursor = conn.cursor()
    
    cursor.execute("SELECT SUM(starting_balance) FROM accounts WHERE user_id = ?", (user_id,))
    starting_balance = cursor.fetchone()[0] or 0.0
    
    cursor.execute("SELECT type, SUM(amount) FROM transactions WHERE user_id = ? AND month_year = ? GROUP BY type", (user_id, month_year))
    tx_totals = {row[0].lower(): row[1] for row in cursor.fetchall()}
    actual_income = tx_totals.get('credit', 0.0)
    actual_expense = tx_totals.get('debit', 0.0)
    
    cursor.execute('''SELECT b.id, b.category, b.amount AS planned, b.type, COALESCE(SUM(t.amount), 0) AS actual
        FROM budgets b LEFT JOIN transactions t ON b.category = t.category AND t.user_id = b.user_id AND t.month_year = b.month_year
        WHERE b.user_id = ? AND b.month_year = ? GROUP BY b.category, b.type''', (user_id, month_year))
    rows = cursor.fetchall()
    conn.close()
    
    budgets = []
    planned_income, planned_expense = 0, 0
    
    for row in rows:
        b_id, category, planned, b_type, actual = row
        if b_type == 'income':
            planned_income += planned
            remaining = actual - planned
        else:
            planned_expense += planned
            remaining = planned - actual
        budgets.append({"id": b_id, "category": category, "planned": planned, "spent": actual, "remaining": remaining, "type": b_type})
        
    return {
        "starting_balance": starting_balance, "planned_income": planned_income, "planned_expense": planned_expense,
        "actual_income": actual_income, "actual_expense": actual_expense,
        "current_balance": starting_balance + actual_income - actual_expense,
        "projected_balance": starting_balance + planned_income - planned_expense,
        "budgets": budgets
    }

@app.get("/api/transactions")
def get_transactions(month_year: str, user_id: int = Depends(get_current_user)):
    conn = sqlite3.connect("finance.db")
    cursor = conn.cursor()
    cursor.execute("SELECT date, description, type, amount, category FROM transactions WHERE user_id = ? AND month_year = ? ORDER BY id DESC", (user_id, month_year))
    rows = cursor.fetchall()
    conn.close()
    return [{"date": r[0], "description": r[1], "type": r[2], "amount": r[3], "category": r[4]} for r in rows]

@app.post("/api/upload-budget")
async def upload_budget_file(month_year: str = Form(...), file: UploadFile = File(...), user_id: int = Depends(get_current_user)):
    try:
        content = await file.read()
        reader = csv.DictReader(io.StringIO(content.decode('utf-8')))
        headers = reader.fieldnames
        cat_col = next((h for h in headers if "category" in h.lower()), None)
        amt_col = next((h for h in headers if "amount" in h.lower() or "planned" in h.lower()), None)
        
        conn = sqlite3.connect("finance.db")
        cursor = conn.cursor()
        count = 0
        for row in reader:
            cat = row.get(cat_col, "").strip()
            amt = str(row.get(amt_col, "0")).replace(",", "").strip()
            if not cat or not amt: continue
            try:
                cursor.execute("SELECT id FROM budgets WHERE user_id=? AND category=? AND month_year=?", (user_id, cat, month_year))
                if cursor.fetchone():
                    cursor.execute("UPDATE budgets SET amount=? WHERE user_id=? AND category=? AND month_year=?", (float(amt), user_id, cat, month_year))
                else:
                    cursor.execute("INSERT INTO budgets (user_id, category, amount, month_year) VALUES (?, ?, ?, ?)", (user_id, cat, float(amt), month_year))
                count += 1
            except ValueError: continue
        conn.commit()
        conn.close()
        return {"message": f"Successfully imported {count} budget categories to {month_year}!"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/upload-statement")
async def upload_statement(month_year: str = Form(...), file: UploadFile = File(...), user_id: int = Depends(get_current_user)):
    content = await file.read()
    short_csv = "\n".join(content.decode('utf-8').split('\n')[:150])
    
    conn = sqlite3.connect("finance.db")
    cursor = conn.cursor()
    cursor.execute("SELECT category FROM budgets WHERE user_id = ? AND month_year = ?", (user_id, month_year))
    categories = [row[0] for row in cursor.fetchall()]
    if not categories: categories = ["Other"]

    prompt = f"""
    You are a financial AI parsing HDFC CSV data.
    CRITICAL: Map EVERY transaction ONLY to one of these categories: {categories}. If it absolutely doesn't fit, use "Other".
    Return EXCLUSIVELY a JSON array of objects with keys: "date", "description", "amount", "type" (debit/credit), "category".
    CSV Data: {short_csv}
    """
    try:
        # Call the active AI provider dynamically
        response = ai_client.chat.completions.create(
            model=ai_settings["model"],
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}
        )
        
        clean_text = response.choices[0].message.content.strip()
        ai_data = json.loads(clean_text)
        
        transactions = ai_data if isinstance(ai_data, list) else ai_data.get("transactions", [])
        
        new_count, dup_count = 0, 0
        for txn in transactions:
            amt = float(str(txn.get("amount", "0")).replace(",", ""))
            date, desc, cat, t_type = txn.get("date", ""), txn.get("description", ""), txn.get("category", "Other"), txn.get("type", "debit")
            
            cursor.execute("SELECT id FROM transactions WHERE user_id=? AND date=? AND description=? AND amount=? AND month_year=?", (user_id, date, desc, amt, month_year))
            if not cursor.fetchone():
                cursor.execute("INSERT INTO transactions (user_id, date, description, amount, category, type, month_year) VALUES (?, ?, ?, ?, ?, ?, ?)", 
                               (user_id, date, desc, amt, cat, t_type, month_year))
                new_count += 1
            else:
                dup_count += 1
        conn.commit()
        conn.close()
        return {"status": "success", "message": f"Saved {new_count} new entries, ignored {dup_count} duplicates."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/clear-data")
def clear_data_advanced(req: ClearDataRequest, user_id: int = Depends(get_current_user)):
    conn = sqlite3.connect("finance.db")
    cursor = conn.cursor()
    if req.transactions: cursor.execute("DELETE FROM transactions WHERE user_id = ? AND month_year = ?", (user_id, req.month_year))
    if req.budgets: cursor.execute("DELETE FROM budgets WHERE user_id = ? AND month_year = ?", (user_id, req.month_year))
    if req.accounts: cursor.execute("DELETE FROM accounts WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    return {"message": "Selected data cleared successfully!"}

@app.post("/api/ask-ai")
def ask_ai_analyst(req: AskAIRequest, user_id: int = Depends(get_current_user)):
    try:
        prompt = f"""
        You are a data analyst. I have a SQLite database:
        1. transactions (id, user_id, date, description, amount, category, type, month_year)
        2. budgets (id, user_id, category, amount, type, month_year)
        
        The user asks: "{req.question}"
        
        Write a SQL query for user_id = {user_id} AND month_year = '{req.month_year}'. 
        Return EXCLUSIVELY JSON: "sql" (the query), "chart_type" (bar/pie/line), "x_col", "y_col".
        """
        response = ai_client.chat.completions.create(
            model=ai_settings["model"],
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}
        )
        
        ai_instructions = json.loads(response.choices[0].message.content.strip())
        
        conn = sqlite3.connect("finance.db")
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(ai_instructions.get("sql"))
        rows = cursor.fetchall()
        conn.close()
        
        labels, data = [], []
        for row in rows:
            row_dict = dict(row)
            keys = list(row_dict.keys())
            if len(keys) >= 2:
                labels.append(str(row_dict[keys[0]]))
                data.append(float(row_dict[keys[1]]))
            elif len(keys) == 1:
                labels.append("Result")
                data.append(float(row_dict[keys[0]]))
                
        return {"status": "success", "chart_type": ai_instructions.get("chart_type", "bar"), "labels": labels, "data": data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))