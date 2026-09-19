"""PostgreSQL Relational Core State Models (SQLAlchemy ORM).

Represents transactional core domain: Users, Orders, and Tickets.
Strictly enforced foreign keys, primary keys, and B-tree indexes.
Designed to support the 'Expand and Contract' zero-downtime evolution pattern.
"""

from sqlalchemy import (
    Column,
    Integer,
    String,
    Numeric,
    DateTime,
    ForeignKey,
    Index,
    CheckConstraint,
    text,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.core.database import Base


class User(Base):
    """Core account data for fans, organizers, and platform administrators."""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(255), unique=True, nullable=False)
    username = Column(String(100), unique=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    full_name = Column(String(255), nullable=False)
    role = Column(String(50), nullable=False, default="fan", server_default="fan")
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    orders = relationship("Order", back_populates="user", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<User(id={self.id}, username='{self.username}', role='{self.role}')>"


class Order(Base):
    """Transactional billing, payment state, and booking lifecycle."""
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_number = Column(String(64), unique=True, nullable=False)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    total_amount = Column(Numeric(10, 2), nullable=False)
    status = Column(
        String(50),
        nullable=False,
        default="pending",
        server_default="pending",
        index=True,
    )  # pending, confirmed, cancelled, refunded
    payment_method = Column(
        String(50),
        nullable=False,
        default="credit_card",
        server_default="credit_card",
    )
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    user = relationship("User", back_populates="orders")
    tickets = relationship("Ticket", back_populates="order", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint("total_amount >= 0", name="chk_order_total_amount_positive"),
        Index("idx_orders_user_status", "user_id", "status"),
    )

    def __repr__(self):
        return f"<Order(id={self.id}, order_number='{self.order_number}', status='{self.status}')>"


class Ticket(Base):
    """Exact seating and zone allocations tied to orders to prevent double-booking."""
    __tablename__ = "tickets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(
        Integer,
        ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # References MongoDB Event document ID (cross-database relational pointer)
    event_id = Column(String(64), nullable=False, index=True)
    ticket_code = Column(String(64), unique=True, nullable=False)
    seat_zone = Column(String(50), nullable=False, index=True)
    seat_number = Column(String(50), nullable=True)
    price = Column(Numeric(10, 2), nullable=False)
    status = Column(
        String(50),
        nullable=False,
        default="valid",
        server_default="valid",
        index=True,
    )  # valid, used, refunded, cancelled
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # Relationships
    order = relationship("Order", back_populates="tickets")

    __table_args__ = (
        CheckConstraint("price >= 0", name="chk_ticket_price_positive"),
        Index("idx_tickets_event_zone", "event_id", "seat_zone"),
        # Partial unique index enforcing double-booking prevention for valid seated tickets
        Index(
            "uq_tickets_event_zone_seat",
            "event_id",
            "seat_zone",
            "seat_number",
            unique=True,
            postgresql_where=text("seat_number IS NOT NULL AND status = 'valid'"),
        ),
    )

    def __repr__(self):
        return f"<Ticket(id={self.id}, code='{self.ticket_code}', event_id='{self.event_id}')>"
