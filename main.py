
import os
import certifi

# Force Python on Mac/VPN to use trusted internet certificates
os.environ["SSL_CERT_FILE"] = certifi.where()
os.environ["SSL_CERT_DIR"] = certifi.where()

from fastapi import FastAPI, HTTPException, UploadFile, File, Depends
from fastapi.responses import FileResponse
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from passlib.context import CryptContext
from jose import JWTError, jwt
from datetime import datetime, timedelta
import sqlite3
import google.generativeai as genai
import csv
import io
from typing import Optional
from dotenv import load_dotenv
import json


load_dotenv()

# Configure Gemini (Replace with your actual key!)
genai.configure(api_key=os.getenv("GCP_API_KEY"))
app = FastAPI(title="AI Finance App - Multi-User")

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

def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

# --- DEPENDENCY: GET LOGGED IN USER ---
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
    return user[0] # Returns the user_id integer!

# --- DATABASE SETUP (Now with user_id) ---
def init_db():
    conn = sqlite3.connect("finance.db")
    cursor = conn.cursor()
    
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL)''')
        
    cursor.execute('''CREATE TABLE IF NOT EXISTS accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, name TEXT NOT NULL, starting_balance REAL NOT NULL,
        UNIQUE(user_id, name))''')
        
    cursor.execute('''CREATE TABLE IF NOT EXISTS budgets (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, category TEXT NOT NULL, amount REAL NOT NULL, type TEXT DEFAULT 'expense',
        UNIQUE(user_id, category))''')
        
    cursor.execute('''CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, date TEXT, description TEXT, amount REAL, category TEXT, type TEXT DEFAULT 'debit')''')

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
        cursor.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)", 
                       (user.username, get_password_hash(user.password)))
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
        
    access_token = create_access_token(data={"sub": form_data.username})
    return {"access_token": access_token, "token_type": "bearer"}

# --- PROTECTED APP ENDPOINTS ---
@app.get("/")
def serve_homepage():
    return FileResponse("index.html")

class AccountEntry(BaseModel):
    name: str
    starting_balance: float

class BudgetEntry(BaseModel):
    category: str
    amount: float
    type: str = "expense"

class ClearDataRequest(BaseModel):
    transactions: bool
    budgets: bool
    accounts: bool

class AskAIRequest(BaseModel):
    question: str

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
    cursor.execute('''INSERT INTO budgets (user_id, category, amount, type) VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id, category) DO UPDATE SET amount=excluded.amount''', 
        (user_id, entry.category, entry.amount, entry.type))
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
def get_budget_summary(user_id: int = Depends(get_current_user)):
    conn = sqlite3.connect("finance.db")
    cursor = conn.cursor()
    
    cursor.execute("SELECT SUM(starting_balance) FROM accounts WHERE user_id = ?", (user_id,))
    starting_balance = cursor.fetchone()[0] or 0.0
    
    cursor.execute("SELECT type, SUM(amount) FROM transactions WHERE user_id = ? GROUP BY type", (user_id,))
    tx_totals = {row[0].lower(): row[1] for row in cursor.fetchall()}
    actual_income = tx_totals.get('credit', 0.0)
    actual_expense = tx_totals.get('debit', 0.0)
    
    cursor.execute('''SELECT b.id, b.category, b.amount AS planned, b.type, COALESCE(SUM(t.amount), 0) AS actual
        FROM budgets b LEFT JOIN transactions t ON b.category = t.category AND t.user_id = b.user_id
        WHERE b.user_id = ? GROUP BY b.category, b.type''', (user_id,))
    rows = cursor.fetchall()
    conn.close()
    
    budgets = []
    planned_income = 0
    planned_expense = 0
    
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

@app.post("/api/upload-budget")
async def upload_budget_file(file: UploadFile = File(...), user_id: int = Depends(get_current_user)):
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
                cursor.execute('''INSERT INTO budgets (user_id, category, amount) VALUES (?, ?, ?)
                    ON CONFLICT(user_id, category) DO UPDATE SET amount=excluded.amount''', (user_id, cat, float(amt)))
                count += 1
            except ValueError: continue
        conn.commit()
        conn.close()
        return {"message": f"Successfully imported {count} budget categories!"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/upload-statement")
async def upload_statement(file: UploadFile = File(...), user_id: int = Depends(get_current_user)):
    content = await file.read()
    short_csv = "\n".join(content.decode('utf-8').split('\n')[:150])
    
    conn = sqlite3.connect("finance.db")
    cursor = conn.cursor()
    cursor.execute("SELECT category FROM budgets WHERE user_id = ?", (user_id,))
    categories = [row[0] for row in cursor.fetchall()]
    if not categories: categories = ["Groceries", "Dining", "Transport", "Bills", "Other"]

    prompt = f"""
    You are a financial AI parsing HDFC CSV data.
    CRITICAL: Map EVERY transaction ONLY to one of these categories: {categories}. If it absolutely doesn't fit, use "Other".
    Return EXCLUSIVELY a JSON array of objects with keys: "date", "description", "amount", "type" (debit/credit), "category".
    CSV Data: {short_csv}
    """
    try:
        model = genai.GenerativeModel('gemini-3.5-flash', generation_config={"response_mime_type": "application/json"})
        response = model.generate_content(prompt)
        
        # Clean up the text just in case Gemini adds formatting
        clean_text = response.text.strip().replace("```json", "").replace("```", "")
        ai_data = json.loads(clean_text)
        
        # Safely handle whether Gemini returns a dictionary or directly returns the list!
        if isinstance(ai_data, list):
            transactions = ai_data
        else:
            transactions = ai_data.get("transactions", [])
        
        new_count, dup_count = 0, 0
        for txn in transactions:
            amt = float(str(txn.get("amount", "0")).replace(",", ""))
            date, desc, cat, t_type = txn.get("date", ""), txn.get("description", ""), txn.get("category", "Other"), txn.get("type", "debit")
            
            cursor.execute("SELECT id FROM transactions WHERE user_id=? AND date=? AND description=? AND amount=?", (user_id, date, desc, amt))
            if not cursor.fetchone():
                cursor.execute("INSERT INTO transactions (user_id, date, description, amount, category, type) VALUES (?, ?, ?, ?, ?, ?)", 
                               (user_id, date, desc, amt, cat, t_type))
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
    if req.transactions: cursor.execute("DELETE FROM transactions WHERE user_id = ?", (user_id,))
    if req.budgets: cursor.execute("DELETE FROM budgets WHERE user_id = ?", (user_id,))
    if req.accounts: cursor.execute("DELETE FROM accounts WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    return {"message": "Selected data cleared successfully!"}

@app.post("/api/ask-ai")
def ask_ai_analyst(req: AskAIRequest, user_id: int = Depends(get_current_user)):
    try:
        prompt = f"""
        You are a data analyst. I have a SQLite database:
        1. transactions (id, user_id, date, description, amount, category, type)
        2. budgets (id, user_id, category, amount, type)
        
        The user asks: "{req.question}"
        
        Write a SQL query for user_id = {user_id}. 
        Return EXCLUSIVELY JSON: "sql" (the query), "chart_type" (bar/pie/line), "x_col", "y_col".
        """
        model = genai.GenerativeModel('gemini-3.5-flash', generation_config={"response_mime_type": "application/json"})
        response = model.generate_content(prompt)
        ai_instructions = json.loads(response.text)
        
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