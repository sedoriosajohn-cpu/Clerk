DROP TABLE IF EXISTS raw_inputs; 

CREATE TABLE raw_inputs (
    raw_id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    source_type TEXT,
    received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

DROP TABLE IF EXISTS tasks;

CREATE TABLE tasks (
    task_id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_id INTEGER,
    task_text TEXT NOT NULL,
    deadline TIMESTAMP,
    confidence INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (raw_id) REFERENCES raw_inputs(raw_id)
);
