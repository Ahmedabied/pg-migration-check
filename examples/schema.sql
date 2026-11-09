-- A table with live traffic: 200,000 orders.
create table orders (id bigserial primary key, email text, qty int, created_at timestamptz default now());
insert into orders (email, qty) select 'u' || g || '@example.com', g % 7 from generate_series(1, 200000) g;
analyze orders;
