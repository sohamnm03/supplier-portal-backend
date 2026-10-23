-- WhatsApp phone verification at signup (see phone_verification.py). Run once on the portal database.
-- code_hash is an HMAC of "<verification_id>:<code>", never the code itself.
CREATE TABLE IF NOT EXISTS phone_verification (
  verification_id CHAR(36)         NOT NULL,
  phone           VARCHAR(20)      NOT NULL,
  code_hash       CHAR(64)         NOT NULL,
  attempts        TINYINT UNSIGNED NOT NULL DEFAULT 0,
  created_at      DATETIME         NOT NULL DEFAULT CURRENT_TIMESTAMP,
  expires_at      DATETIME         NOT NULL,
  verified_at     DATETIME         NULL,
  superseded_at   DATETIME         NULL,
  PRIMARY KEY (verification_id),
  KEY idx_phone_verification_phone_created (phone, created_at)
);
