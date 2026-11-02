SYSTEM_VARS = {"_frontend_url": "http://localhost:9001"}
DB = {
    "provider": "postgres",
    "user": "FILLME",
    "password": "FILLME",
    "host": "localhost",
    "database": "bpmn_praksa",
}
DS = {
    "baserow": {"type": "http-connector", "url": "http://0.0.0.0:8080"},
    "emailjs": {"type": "http-connector", "url": "http://localhost:8083"},
    "pdf": {"type": "http-connector", "url": "http://0.0.0.0:8083"},
}
