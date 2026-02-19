CREATE TABLE raw_inputs (
    raw_id SERIAL PRIMARY KEY,
    content TEXT NOT NULL,
    source_type TEXT,
    received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

