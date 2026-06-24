from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Float, DateTime, ForeignKey, Text, Boolean
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class Market(Base):
    __tablename__ = "markets"

    id = Column(Integer, primary_key=True)
    condition_id = Column(String, unique=True, nullable=False, index=True)
    question = Column(Text, nullable=False)
    category = Column(String, default="")
    yes_price = Column(Float, nullable=False)   # 0–1 implied probability
    no_price = Column(Float, nullable=False)
    volume_24h = Column(Float, default=0.0)
    open_interest = Column(Float, default=0.0)
    resolution_date = Column(DateTime, nullable=True)
    description = Column(Text, default="")
    active = Column(Boolean, default=True)
    last_updated = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    opportunities = relationship("Opportunity", back_populates="market", cascade="all, delete-orphan")
    trades = relationship("Trade", back_populates="market", cascade="all, delete-orphan")
    price_history = relationship("PriceHistory", back_populates="market", cascade="all, delete-orphan")
    predictions = relationship("Prediction", back_populates="market", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Market {self.condition_id[:8]}… YES={self.yes_price:.2f}>"


class Opportunity(Base):
    __tablename__ = "opportunities"

    id = Column(Integer, primary_key=True)
    market_id = Column(Integer, ForeignKey("markets.id"), nullable=False, index=True)
    implied_prob = Column(Float, nullable=False)
    estimated_prob = Column(Float, nullable=False)
    edge = Column(Float, nullable=False)           # estimated_prob - implied_prob
    ev = Column(Float, nullable=False)             # expected value as a fraction
    kelly_fraction = Column(Float, nullable=False)
    position_size_usd = Column(Float, nullable=False)
    recommended_side = Column(String, nullable=False)  # "YES" or "NO"
    confidence = Column(String, default="medium")       # low / medium / high
    evidence_summary = Column(Text, default="")
    key_factors = Column(Text, default="")
    risks = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    scan_run_id = Column(Integer, ForeignKey("scan_runs.id"), nullable=True)

    market = relationship("Market", back_populates="opportunities")
    scan_run = relationship("ScanRun", back_populates="opportunities")

    prediction = relationship("Prediction", back_populates="opportunity", uselist=False)

    def __repr__(self) -> str:
        return f"<Opportunity market={self.market_id} side={self.recommended_side} EV={self.ev:.3f}>"


class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True)
    market_id = Column(Integer, ForeignKey("markets.id"), nullable=False, index=True)
    side = Column(String, nullable=False)        # "YES" or "NO"
    price = Column(Float, nullable=False)
    size_usd = Column(Float, nullable=False)
    status = Column(String, default="open")      # open / closed / cancelled
    opened_at = Column(DateTime, default=datetime.utcnow)
    closed_at = Column(DateTime, nullable=True)
    pnl = Column(Float, nullable=True)
    notes = Column(Text, default="")

    market = relationship("Market", back_populates="trades")

    def __repr__(self) -> str:
        return f"<Trade {self.side} ${self.size_usd:.2f} @ {self.price:.3f} [{self.status}]>"


class ScanRun(Base):
    __tablename__ = "scan_runs"

    id = Column(Integer, primary_key=True)
    started_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)
    markets_scanned = Column(Integer, default=0)
    opportunities_found = Column(Integer, default=0)
    errors = Column(Integer, default=0)
    duration_seconds = Column(Float, nullable=True)

    opportunities = relationship("Opportunity", back_populates="scan_run")

    def __repr__(self) -> str:
        return f"<ScanRun id={self.id} markets={self.markets_scanned} ops={self.opportunities_found}>"


class Prediction(Base):
    """One prediction per unique (condition_id, predicted_side). Tracks WIN/LOSS vs market outcome."""
    __tablename__ = "predictions"

    id = Column(Integer, primary_key=True)
    market_id = Column(Integer, ForeignKey("markets.id"), nullable=False, index=True)
    opportunity_id = Column(Integer, ForeignKey("opportunities.id"), nullable=True)
    condition_id = Column(String, nullable=False, index=True)
    question = Column(Text, nullable=False)
    predicted_side = Column(String, nullable=False)   # YES / NO
    predicted_prob = Column(Float, nullable=False)    # our Claude estimate
    implied_prob = Column(Float, nullable=False)      # market price at prediction time
    ev = Column(Float, nullable=False)
    confidence = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    resolved_at = Column(DateTime, nullable=True)
    outcome = Column(String, default="PENDING")       # PENDING / WIN / LOSS
    resolution_value = Column(String, nullable=True)  # YES / NO as returned by Polymarket

    market = relationship("Market", back_populates="predictions")
    opportunity = relationship("Opportunity", back_populates="prediction")

    def __repr__(self) -> str:
        return f"<Prediction {self.predicted_side} [{self.outcome}] EV={self.ev:.2f}>"


class PriceHistory(Base):
    __tablename__ = "price_history"

    id = Column(Integer, primary_key=True)
    market_id = Column(Integer, ForeignKey("markets.id"), nullable=False, index=True)
    timestamp = Column(DateTime, nullable=False, index=True)
    yes_price = Column(Float, nullable=False)
    no_price = Column(Float, nullable=False)
    volume = Column(Float, default=0.0)

    market = relationship("Market", back_populates="price_history")
