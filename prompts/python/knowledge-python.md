# Python Knowledge Base

## About This File
This file contains Python-specific knowledge for agents working on Python projects.

## Validation (REQUIRED)

### Python Validation Commands
```bash
# Syntax check
python3 -m py_compile <file.py>

# Lint with ruff
ruff check <file.py>

# Format with black
black <file.py>

# Run tests
pytest

# Type check
mypy <file.py>
```

### NEVER do:
- DO NOT use Python 2 syntax
- DO NOT use outdated packages

---

## Python Best Practices

### FastAPI Patterns
```python
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI()

class Item(BaseModel):
    name: str
    description: str | None = None
    price: float
    tax: float | None = None

@app.post("/items/")
async def create_item(item: Item):
    return item

@app.get("/items/{item_id}")
async def read_item(item_id: int):
    return {"item_id": item_id}
```

### Class Structure
```python
class GameManager:
    def __init__(self):
        self.games = {}
    
    def create_game(self, game_id: str) -> Game:
        game = Game(game_id)
        self.games[game_id] = game
        return game
    
    def get_game(self, game_id: str) -> Game | None:
        return self.games.get(game_id)
```

### Async/Await
```python
import asyncio

async def fetch_data():
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            return await response.json()
```

---

## Common Libraries

| Use Case | Library |
|----------|---------|
| Web API | FastAPI, Flask |
| Database | SQLAlchemy, asyncpg |
| Validation | Pydantic |
| HTTP | requests, aiohttp |
| Websockets | websockets |
| Testing | pytest, pytest-asyncio |
| Linting | ruff, flake8 |
| Formatting | black, isort |

---

## Project Structure
```
project/
├── main.py              # Entry point
├── api/                 # API routes
│   └── routes.py
├── models/              # Data models
│   └── item.py
├── services/            # Business logic
│   └── game.py
├── database.py          # Database setup
├── requirements.txt
└── tests/
    └── test_api.py
```
