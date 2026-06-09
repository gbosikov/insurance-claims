"""ORM-модель: справочник провайдеров (клиник)."""

from sqlalchemy import Boolean, Column, DateTime, Index, Integer, String, Text, func

from core.database import Base


class Provider(Base):
    __tablename__ = "providers"

    id          = Column(Integer, primary_key=True)
    customer_id = Column(Integer, nullable=False, unique=True, index=True)  # PersID из кор-системы
    cstname     = Column(Text, nullable=False)                              # имя клиники
    taxpayer    = Column(String(50))                                        # ИНН провайдера
    is_active   = Column(Boolean, nullable=False, default=True)
    updated_at  = Column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<Provider {self.customer_id} name={self.cstname!r}>"
