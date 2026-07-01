import os
from sqlalchemy import create_engine

def print_sqlalchemy_args_with_uri_param():
    db_path = "sqlite:///file:org_test_temp_sa?mode=memory&cache=shared&uri=true"
    engine = create_engine(db_path)
    
    print("Engine URL database path:", engine.url.database)
    print("Engine URL query parameters:", engine.url.query)
    
    conn = engine.raw_connection()
    print("Files matching org_test_temp_sa during connection:", [f for f in os.listdir(".") if "org_test_temp_sa" in f])
    conn.close()
    print("Files matching org_test_temp_sa after close:", [f for f in os.listdir(".") if "org_test_temp_sa" in f])
    
    # Clean up
    for f in os.listdir("."):
        if "org_test_temp_sa" in f:
            os.remove(f)

print_sqlalchemy_args_with_uri_param()
