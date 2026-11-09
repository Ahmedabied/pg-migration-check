-- What `python manage.py sqlmigrate shop 0002` might print: looks harmless, is not.
BEGIN;
ALTER TABLE "orders" ALTER COLUMN "qty" TYPE bigint;
CREATE INDEX "orders_email_idx" ON "orders" ("email");
ALTER TABLE "orders" ALTER COLUMN "email" SET NOT NULL;
ALTER TABLE "orders" RENAME COLUMN "created_at" TO "placed_at";
COMMIT;
