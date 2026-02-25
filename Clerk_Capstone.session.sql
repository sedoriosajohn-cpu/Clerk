DROP TABLE IF EXISTS raw_inputs; 

CREATE TABLE raw_inputs (
    raw_id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    source_type TEXT,
    received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

