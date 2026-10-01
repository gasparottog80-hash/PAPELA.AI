-- Serializes explicit migration invocations for this application/database.
SELECT pg_advisory_xact_lock(7061, 4);
