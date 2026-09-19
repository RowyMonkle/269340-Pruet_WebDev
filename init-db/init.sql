-- ==============================================================================
-- 269340 Ticket Booking Platform - PostgreSQL DDL Baseline
-- Checkpoint 1 & Tier 1 Enhancements: Users, Orders, Tickets, Payments, Outbox
-- Follows Zero-Downtime Expand-and-Contract Architecture Patterns
-- ==============================================================================

-- 1. Users Table (Core accounts for fans, organizers, and admins)
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    email VARCHAR(255) NOT NULL UNIQUE,
    username VARCHAR(100) NOT NULL UNIQUE,
    hashed_password VARCHAR(255) NOT NULL,
    full_name VARCHAR(255) NOT NULL,
    role VARCHAR(50) NOT NULL DEFAULT 'fan',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL
);

-- Index for ordering and pagination (UNIQUE columns email and username already have implicit unique indexes)
CREATE INDEX IF NOT EXISTS idx_users_created_at ON users(created_at DESC);

-- 2. Orders Table (Billing lifecycle, seat holds with expiry, and idempotency)
CREATE TABLE IF NOT EXISTS orders (
    id SERIAL PRIMARY KEY,
    order_number VARCHAR(64) NOT NULL UNIQUE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    total_amount NUMERIC(10, 2) NOT NULL CHECK (total_amount >= 0),
    status VARCHAR(50) NOT NULL DEFAULT 'pending', -- pending (held), paid, confirmed, expired, cancelled
    payment_method VARCHAR(50) NOT NULL DEFAULT 'promptpay',
    expires_at TIMESTAMP WITH TIME ZONE, -- Seat hold expiry (e.g. 10 minutes)
    idempotency_key VARCHAR(128) UNIQUE, -- Protects against duplicate submissions
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL
);

-- Indexes for user orders, status, and expiry checks
CREATE INDEX IF NOT EXISTS idx_orders_user_id ON orders(user_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_expires_at ON orders(expires_at);
CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_user_status ON orders(user_id, status);
CREATE INDEX IF NOT EXISTS idx_orders_status_expires ON orders(status, expires_at);

-- 3. Tickets Table (Exact seating/zone allocations tied to orders to prevent double-booking)
CREATE TABLE IF NOT EXISTS tickets (
    id SERIAL PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    event_id VARCHAR(64) NOT NULL, -- Cross-reference to MongoDB Event ObjectId/String
    ticket_code VARCHAR(64) NOT NULL UNIQUE,
    seat_zone VARCHAR(50) NOT NULL,
    seat_number VARCHAR(50),
    price NUMERIC(10, 2) NOT NULL CHECK (price >= 0),
    status VARCHAR(50) NOT NULL DEFAULT 'held', -- held, valid, used, refunded, cancelled
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL
);

-- Foreign key and event lookups
CREATE INDEX IF NOT EXISTS idx_tickets_order_id ON tickets(order_id);
CREATE INDEX IF NOT EXISTS idx_tickets_event_id ON tickets(event_id);
CREATE INDEX IF NOT EXISTS idx_tickets_event_zone ON tickets(event_id, seat_zone);

-- Partial Unique Index: Strictly enforces NO double-booking for any ticket currently held or valid
CREATE UNIQUE INDEX IF NOT EXISTS uq_tickets_event_zone_seat 
ON tickets (event_id, seat_zone, seat_number) 
WHERE seat_number IS NOT NULL AND status IN ('held', 'valid');

-- 4. Payments Table (Rich relational billing with 1:N payment attempts per order)
CREATE TABLE IF NOT EXISTS payments (
    id SERIAL PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    payment_reference VARCHAR(64) NOT NULL UNIQUE,
    amount NUMERIC(10, 2) NOT NULL CHECK (amount >= 0),
    provider VARCHAR(50) NOT NULL DEFAULT 'promptpay', -- promptpay, credit_card, stripe, bank_transfer
    status VARCHAR(50) NOT NULL DEFAULT 'pending', -- pending, completed, failed, refunded
    provider_tx_id VARCHAR(128),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_payments_order_id ON payments(order_id);
CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);
CREATE INDEX IF NOT EXISTS idx_payments_order_status ON payments(order_id, status);

-- 5. Outbox Events Table (Transactional Outbox Pattern for Dual-DB Consistency)
CREATE TABLE IF NOT EXISTS outbox_events (
    id SERIAL PRIMARY KEY,
    event_type VARCHAR(64) NOT NULL, -- SEAT_HELD, SEAT_RELEASED, ORDER_PAID, ORDER_EXPIRED
    aggregate_type VARCHAR(64) NOT NULL DEFAULT 'order',
    aggregate_id VARCHAR(64) NOT NULL,
    payload TEXT NOT NULL, -- Serialized JSON event data
    status VARCHAR(50) NOT NULL DEFAULT 'pending', -- pending, processed, failed
    retry_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    processed_at TIMESTAMP WITH TIME ZONE
);

CREATE INDEX IF NOT EXISTS idx_outbox_status_created ON outbox_events(status, created_at);
CREATE INDEX IF NOT EXISTS idx_outbox_event_type ON outbox_events(event_type);
CREATE INDEX IF NOT EXISTS idx_outbox_aggregate_id ON outbox_events(aggregate_id);
