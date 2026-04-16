from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Post:
    title: str
    source_url: str
    source_site: str
    content: Optional[str] = None
    image_url: Optional[str] = None
    upvotes: int = 0
    comments: int = 0
    views: int = 0
    created_at: Optional[datetime] = None
    score: float = 0.0
    img_hash: Optional[str] = None
