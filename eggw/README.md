# eggw - Web UI for eggthreads

Web interface for managing AI conversation threads using eggthreads backend.

## Architecture

- **Backend**: FastAPI (Python) with direct eggthreads integration
- **Frontend**: Next.js 14 with React, TypeScript, and Tailwind CSS
- **Communication**: REST API + SSE for streaming + WebSocket for real-time

## Quick Start

### Backend

```bash
cd backend
pip install -r requirements.txt
hypercorn main:app --bind 0.0.0.0:8000
```

Note: We use hypercorn with HTTP/2 support to allow multiple browser tabs to view the same thread simultaneously (HTTP/1.1 has a ~6 connection limit per origin).

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Then open http://localhost:3000

## Features

- Thread tree navigation with parent/child hierarchy
- Real-time message streaming via SSE
- Tool call approval UI
- Model selection per thread
- Token usage statistics
- Live TPS in the Chat Messages header during LLM streaming
- Dark theme

## API Endpoints

### REST

- `GET /api/threads` - List all threads
- `GET /api/threads/{id}` - Get thread details
- `POST /api/threads` - Create new thread
- `DELETE /api/threads/{id}` - Delete thread
- `GET /api/threads/{id}/messages` - Get messages
- `POST /api/threads/{id}/messages` - Send message
- `GET /api/models` - List available models
- `POST /api/threads/{id}/model` - Set thread model
- `GET /api/threads/{id}/tools` - Get tool calls
- `POST /api/threads/{id}/tools/approve` - Approve/deny tool

### SSE

- `GET /api/threads/{id}/events` - Stream thread events

### WebSocket

- `WS /ws/{id}` - Real-time bidirectional communication

## Development

The backend expects eggthreads and eggllm to be available in the parent directory.
Copy `models.json` from the egg directory or create your own configuration.
