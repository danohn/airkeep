ALTER TABLE airkeep_data.outbox ADD COLUMN lease_until double precision NOT NULL DEFAULT 0;
ALTER TABLE airkeep_data.activities ADD COLUMN lease_until double precision NOT NULL DEFAULT 0;
