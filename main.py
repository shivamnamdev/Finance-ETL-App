from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
import sqlite3
import google.generativeai as genai
import csv
import io
from pydantic import BaseModel
from typing import Optional

# Configure Gemini (Replace with your actual key!)
genai.configure(api_key="AQ.Ab8RN6LMGdRiaXb_OeDFRgpueHRGGA3YwEplR1eXAJQCYFxXvg")

app = FastAPI(title="AI Finance App - Week 1")

# Allow frontend to communicate with backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- DATABASE SETUP ---
def init_db():
    conn = sqlite3.connect("finance.db")
    cursor = conn.cursor()
    
    # 1. Accounts Table (NEW)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            starting_balance REAL NOT NULL
        )
    ''')
    
    # 2. Budgets Table (Updated with 'type')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS budgets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT UNIQUE NOT NULL,
            amount REAL NOT NULL,
            type TEXT DEFAULT 'expense'
        )
    ''')
    
    # Safely migrate existing databases to have the 'type' column
    try:
        cursor.execute("ALTER TABLE budgets ADD COLUMN type TEXT DEFAULT 'expense'")
    except sqlite3.OperationalError:
        pass # Column already exists, safe to ignore
        
    # 3. Transactions Table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            description TEXT,
            amount REAL,
            category TEXT,
            type TEXT DEFAULT 'debit'
        )
    ''')
    
    try:
        cursor.execute("ALTER TABLE transactions ADD COLUMN type TEXT DEFAULT 'debit'")
    except sqlite3.OperationalError:
        pass # Column already exists, safe to ignore

    conn.commit()
    conn.close()

init_db()

# --- PYDANTIC MODELS ---
class AccountEntry(BaseModel):
    name: str
    starting_balance: float

class BudgetEntry(BaseModel):
    category: str
    amount: float
    type: str = "expense" # defaults to expense


# --- API ENDPOINTS ---

# --- SERVE THE FRONTEND ---
@app.get("/")
def serve_homepage():
    return FileResponse("index.html")

@app.post("/api/budgets")
def add_budget(entry: BudgetEntry):
    try:
        conn = sqlite3.connect("finance.db")
        cursor = conn.cursor()
        cursor.execute("INSERT INTO budgets (category, amount) VALUES (?, ?)", (entry.category, entry.amount))
        conn.commit()
        conn.close()
        return {"message": "Budget added successfully!"}
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="Category already exists!")

@app.get("/api/budgets")
def get_budgets():
    conn = sqlite3.connect("finance.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, category, amount FROM budgets")
    rows = cursor.fetchall()
    conn.close()
    
    budgets = [{"id": row[0], "category": row[1], "amount": row[2]} for row in rows]
    total_budget = sum(row['amount'] for row in budgets)
    
    return {"budgets": budgets, "total_budget": total_budget}

@app.delete("/api/budgets/{budget_id}")
def delete_budget(budget_id: int):
    conn = sqlite3.connect("finance.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM budgets WHERE id = ?", (budget_id,))
    conn.commit()
    conn.close()
    return {"message": "Budget deleted!"}

@app.post("/api/upload-budget")
async def upload_budget_file(file: UploadFile = File(...)):
    try:
        content = await file.read()
        csv_text = content.decode('utf-8')
        
        # Parse the CSV file
        reader = csv.DictReader(io.StringIO(csv_text))
        headers = reader.fieldnames
        
        if not headers:
            raise HTTPException(status_code=400, detail="Empty CSV file")
            
        # Dynamically find which columns contain the "Category" and "Amount" (case-insensitive)
        category_col = next((h for h in headers if "category" in h.lower()), None)
        amount_col = next((h for h in headers if "amount" in h.lower() or "planned" in h.lower()), None)
        
        if not category_col or not amount_col:
            raise HTTPException(status_code=400, detail="Could not find 'Category' and 'Amount' columns in your CSV header.")

        conn = sqlite3.connect("finance.db")
        cursor = conn.cursor()
        
        count = 0
        for row in reader:
            category = row.get(category_col, "").strip()
            amount_str = str(row.get(amount_col, "0")).replace(",", "").strip()
            
            if not category or not amount_str:
                continue
                
            try:
                amount = float(amount_str)
            except ValueError:
                continue # Skip rows where amount isn't a number
                
            # Insert the budget. If the category already exists, update the amount!
            cursor.execute('''
                INSERT INTO budgets (category, amount) 
                VALUES (?, ?)
                ON CONFLICT(category) DO UPDATE SET amount=excluded.amount
            ''', (category, amount))
            count += 1
            
        conn.commit()
        conn.close()
        
        return {"message": f"Successfully imported {count} budget categories!"}
        
    except Exception as e:
        print(f"🔥 BUDGET UPLOAD CRASHED: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

import os
from fastapi import UploadFile, File
from openai import OpenAI
import json

# Initialize OpenAI Client (You will need an API key)
# Get one at https://platform.openai.com/api-keys
os.environ["OPENAI_API_KEY"] = "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" # Replace with your real key
client = OpenAI()

@app.post("/api/upload-statement")
async def upload_statement(file: UploadFile = File(...)):
    # 1. Read the uploaded CSV file
    content = await file.read()
    csv_text = content.decode('utf-8')
    
    # Let's limit to the first 20 lines for the MVP to save AI tokens/time
    short_csv = "\n".join(csv_text.split('\n')[:])

    # 2. Get your current budget categories from the database
    conn = sqlite3.connect("finance.db")
    cursor = conn.cursor()
    cursor.execute("SELECT category FROM budgets")
    categories = [row[0] for row in cursor.fetchall()]
    conn.close()

    # If no categories exist, provide a fallback
    if not categories:
        categories = ["Groceries", "Dining", "Transport", "Bills", "Other"]

    # 3. Create the AI Prompt
    prompt = f"""
    You are an expert financial AI. I am providing you with an HDFC Bank statement in CSV format.
    The CSV will have columns like: Date, Narration, Chq./Ref.No., Value Dt, Withdrawal Amt., Deposit Amt., Closing Balance.

    Your job is to parse these transactions and map them to these specific budget categories: {categories}.
    If it doesn't fit neatly, use "Unplanned Expense" or "Basic Expense".

    Here are the specific rules for cleaning the data:
    1. HDFC "Narration" for UPI looks like "UPI-NAME-VPA-REMARKS". Extract ONLY the Name and the Remark (e.g., from "UPI-SWIGGY INSTAMART-PRI-SWIGGYINSTAMART@AXB", clean it to "Swiggy Instamart").
    2. Check the "Withdrawal Amt." and "Deposit Amt." columns. 
       - If "Withdrawal Amt." has a value, it is an EXPENSE (type: "debit").
       - If "Deposit Amt." has a value, it is INCOME or COLLECTED MONEY (type: "credit").
    3. Look out for your specific EMIs (e.g., "ACH D- BAJAJ FINANCE", "EMI 119344914").

    Return the result EXCLUSIVELY as a valid JSON array of objects with these keys: 
    "date", "description" (the cleaned name/remark), "amount" (as a positive float), "type" ("debit" or "credit"), and "category".

    Here is the CSV data:
    {short_csv}
    """

    # 4. Call Google Gemini (Free Tier)
    try:
        # We use the 1.5-flash model because it's extremely fast and free
        model = genai.GenerativeModel(
            'gemini-3.5-flash', 
            generation_config={"response_mime_type": "application/json"}
        )
        
        response = model.generate_content(prompt)
        ai_data = json.loads(response.text)
        
        transactions = ai_data.get("transactions", ai_data) if isinstance(ai_data, dict) else ai_data
        
        # --- SAVE TO DB ---
        # --- SAVE TO DB WITH DEDUPLICATION ---
        conn = sqlite3.connect("finance.db")
        cursor = conn.cursor()
        
        new_count = 0
        duplicate_count = 0
        
        for txn in transactions:
            raw_amount = str(txn.get("amount", "0")).replace(",", "")
            amount = float(raw_amount)
            date = txn.get("date", "")
            desc = txn.get("description", "")
            category = txn.get("category", "Other")
            
            # Check if this exact transaction already exists!
            cursor.execute('''
                SELECT id FROM transactions 
                WHERE date = ? AND description = ? AND amount = ?
            ''', (date, desc, amount))
            
            if not cursor.fetchone(): 
                # It does not exist, so let's save it
                cursor.execute(
                    "INSERT INTO transactions (date, description, amount, category) VALUES (?, ?, ?, ?)",
                    (date, desc, amount, category)
                )
                new_count += 1
            else:
                # It's a duplicate, ignore it
                duplicate_count += 1
                
        conn.commit()
        conn.close()
        # -----------------------------------

        # We can print this to the terminal to see the AI working smartly!
        print(f"✅ Saved {new_count} new entries. Ignored {duplicate_count} duplicates.")

        return {"status": "success", "transactions": transactions}
       

    except Exception as e:
        print(f"🔥 BACKEND CRASHED: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))  

# --- ACCOUNTS ENDPOINTS ---
@app.post("/api/accounts")
def add_account(entry: AccountEntry):
    conn = sqlite3.connect("finance.db")
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO accounts (name, starting_balance) VALUES (?, ?)
        ON CONFLICT(name) DO UPDATE SET starting_balance=excluded.starting_balance
    ''', (entry.name, entry.starting_balance))
    conn.commit()
    conn.close()
    return {"message": "Account updated!"}

@app.get("/api/accounts")
def get_accounts():
    conn = sqlite3.connect("finance.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, starting_balance FROM accounts")
    rows = cursor.fetchall()
    conn.close()
    return [{"id": row[0], "name": row[1], "balance": row[2]} for row in rows]

# --- THE CASHFLOW SUMMARY ENGINE (Replaces old /api/summary) ---
@app.get("/api/summary")
def get_budget_summary():
    conn = sqlite3.connect("finance.db")
    cursor = conn.cursor()
    
    # 1. Get Starting Balance
    cursor.execute("SELECT SUM(starting_balance) FROM accounts")
    starting_balance = cursor.fetchone()[0] or 0.0
    
    # 2. Get Actual Spent & Earned directly from Transactions
    cursor.execute("SELECT type, SUM(amount) FROM transactions GROUP BY type")
    tx_totals = {row[0].lower(): row[1] for row in cursor.fetchall()}
    actual_income = tx_totals.get('credit', 0.0)
    actual_expense = tx_totals.get('debit', 0.0)
    
    # 3. Match Budgets with Transactions
    cursor.execute('''
        SELECT 
            b.id, b.category, b.amount AS planned, b.type,
            COALESCE(SUM(t.amount), 0) AS actual
        FROM budgets b
        LEFT JOIN transactions t ON b.category = t.category 
        GROUP BY b.category, b.type
    ''')
    
    rows = cursor.fetchall()
    conn.close()
    
    budgets = []
    planned_income = 0
    planned_expense = 0
    
    for row in rows:
        b_id, category, planned, b_type, actual = row
        if b_type == 'income':
            planned_income += planned
            remaining = actual - planned # Positive is good!
        else:
            planned_expense += planned
            remaining = planned - actual # Positive is good!
            
        budgets.append({
            "id": b_id, "category": category, "planned": planned, 
            "spent": actual, "remaining": remaining, "type": b_type
        })
        
    # THE FINTECH MATH:
    current_balance = starting_balance + actual_income - actual_expense
    projected_balance = starting_balance + planned_income - planned_expense
    
    return {
        "starting_balance": starting_balance,
        "planned_income": planned_income,
        "planned_expense": planned_expense,
        "actual_income": actual_income,
        "actual_expense": actual_expense,
        "current_balance": current_balance,
        "projected_balance": projected_balance,
        "budgets": budgets
    }       

@app.delete("/api/transactions/clear")
def clear_all_transactions():
    conn = sqlite3.connect("finance.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM transactions") # Deletes only transactions, keeps budgets safe!
    conn.commit()
    conn.close()
    return {"message": "All transactions cleared!"}
