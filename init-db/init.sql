-- ==============================================================================
-- 269340 Ticket Booking Platform - PostgreSQL DDL Baseline
-- Checkpoint 1: Normalized Transactional Schema (Users, Orders, Tickets)
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

-- 2. Orders Table (Transactional billing and payment states)
CREATE TABLE IF NOT EXISTS orders (
    id SERIAL PRIMARY KEY,
    order_number VARCHAR(64) NOT NULL UNIQUE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    total_amount NUMERIC(10, 2) NOT NULL CHECK (total_amount >= 0),
    status VARCHAR(50) NOT NULL DEFAULT 'pending',
    payment_method VARCHAR(50) NOT NULL DEFAULT 'credit_card',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL
);

-- Non-unique indexes for user order queries and status filtering
CREATE INDEX IF NOT EXISTS idx_orders_user_id ON orders(user_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_user_status ON orders(user_id, status);

-- 3. Tickets Table (Exact seating/zone allocations tied to orders to prevent double-booking)
CREATE TABLE IF NOT EXISTS tickets (
    id SERIAL PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    event_id VARCHAR(64) NOT NULL, -- Cross-reference to MongoDB Event ObjectId/String
    ticket_code VARCHAR(64) NOT NULL UNIQUE,
    seat_zone VARCHAR(50) NOT NULL,
    seat_number VARCHAR(50),
    price NUMERIC(10, 2) NOT NULL CHECK (price >= 0),
    status VARCHAR(50) NOT NULL DEFAULT 'valid',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL
);

-- Foreign key and event lookups
CREATE INDEX IF NOT EXISTS idx_tickets_order_id ON tickets(order_id);
CREATE INDEX IF NOT EXISTS idx_tickets_event_id ON tickets(event_id);
CREATE INDEX IF NOT EXISTS idx_tickets_event_zone ON tickets(event_id, seat_zone);

-- Partial Unique Index: Strictly enforces NO double-booking for valid tickets with assigned seats
CREATE UNIQUE INDEX IF NOT EXISTS uq_tickets_event_zone_seat 
ON tickets (event_id, seat_zone, seat_number) 
WHERE seat_number IS NOT NULL AND status = 'valid';
