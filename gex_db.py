from datetime import datetime, timedelta

import psycopg2
from psycopg2.extras import Json

# Connection parameters (replace with your details)
DB_NAME = "postgres"
DB_USER = "postgres"
DB_PASSWORD = "mysecretpassword"
DB_HOST = "localhost"
DB_PORT = "5432"

def connect():
    # Establish connection
    conn = psycopg2.connect(
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        host=DB_HOST,
        port=DB_PORT
    )
    return conn

def getDB(target_date):
    conn = connect()
    cur = conn.cursor()

    print(f"Fetching gex table for {target_date:%Y-%m-%d %H:%M:%S}")
    sql_query_text = "SELECT data FROM gex WHERE time = %s;"
    cur.execute(sql_query_text, (target_date,))

    data_dict = None
    data_result = cur.fetchone()
    if data_result:
        # psycopg2 often handles the conversion, but if needed, use json.loads()
        data_dict = data_result[0]

    # Close the cursor and connection
    cur.close()
    conn.close()

    return data_dict

def listDB(target_date):
    start_of_day = datetime.combine(target_date, datetime.min.time())
    end_of_day = start_of_day + timedelta(days=1)

    print(target_date,start_of_day,end_of_day)

    conn = connect()
    try:
        sql = """
              SELECT time, data FROM gex
              WHERE time >= %s AND time < %s; \
              """
        with conn.cursor() as cur:
            # Pass the start and end datetime objects as parameters
            cur.execute(sql, (start_of_day, end_of_day))
            records = cur.fetchall()
            return records

            # return [item[0] for item in records]

    except psycopg2.Error as e:
        print(f"Database error: {e}")
        return []

def saveDB(target_date, data):
    datetime_zero_seconds = target_date.replace(second=0, microsecond=0)

    conn = connect()
    try:
        with conn.cursor() as cur:
            # Use %s as a placeholder for the entire Json object
            insert_sql = "INSERT INTO gex (time, data) VALUES (%s, %s);"

            # Pass the wrapped data as a parameter
            cur.execute(insert_sql, (datetime_zero_seconds, Json(data),))

        conn.commit()
    except Exception as e:
        print(f"Error: {e}")
        conn.rollback()
    finally:
        conn.close()
