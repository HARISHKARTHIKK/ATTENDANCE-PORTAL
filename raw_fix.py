import os
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

# .env file-la irundhu DB URL edukka
load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

# Supabase/PostgreSQL connection setup
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL)

def fix_database():
    print("Direct database connection bypass pannittaen...")
    with engine.connect() as conn:
        try:
            # Add missing columns manually
            print("Columns add panraen...")
            conn.execute(text('ALTER TABLE "user" ADD COLUMN IF NOT EXISTS role VARCHAR(20) DEFAULT \'teacher\';'))
            conn.execute(text('ALTER TABLE "user" ADD COLUMN IF NOT EXISTS assigned_class VARCHAR(50);'))
            conn.commit()
            print("✅ Success! Database schema updated.")
        except Exception as e:
            print(f"❌ Error: {e}")

if __name__ == "__main__":
    fix_database()