ALTER TABLE oauth_grants ADD CONSTRAINT oauth_grants_owner_id UNIQUE ("user", id);
ALTER TABLE oauth_grants DROP CONSTRAINT oauth_grants_parent_fkey;
ALTER TABLE oauth_grants ADD FOREIGN KEY ("user", parent) REFERENCES oauth_grants("user", id) ON DELETE CASCADE;

ALTER TABLE airkeep_data.registration_owners DROP CONSTRAINT registration_owners_grant_id_fkey;
ALTER TABLE airkeep_data.registration_owners ADD FOREIGN KEY ("user", grant_id)
    REFERENCES oauth_grants("user", id) ON DELETE CASCADE;

ALTER TABLE relay_credentials ADD FOREIGN KEY ("user", printer_id)
    REFERENCES airkeep_data.printers(tenant_id, id) ON DELETE CASCADE;
