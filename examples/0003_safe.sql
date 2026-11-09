-- The same intent, done so live traffic keeps flowing.
SET lock_timeout = '2s';
CREATE INDEX CONCURRENTLY orders_qty_idx ON orders (qty);
ALTER TABLE orders ADD CONSTRAINT orders_email_not_null CHECK (email IS NOT NULL) NOT VALID;
ALTER TABLE orders VALIDATE CONSTRAINT orders_email_not_null;
ALTER TABLE orders ALTER COLUMN email SET NOT NULL;
ALTER TABLE orders ADD COLUMN gift boolean NOT NULL DEFAULT false;
