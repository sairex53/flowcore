CREATE TABLE IF NOT EXISTS flowcore_bench (
    id integer PRIMARY KEY,
    value text NOT NULL
);
INSERT INTO flowcore_bench (id, value)
SELECT i, md5(i::text)
FROM generate_series(1, 4096) AS i
ON CONFLICT (id) DO NOTHING;
