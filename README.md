# LiveX Scheduling Assistant

A Python scheduling chatbot backed by Cal.com and Gemini, with a Next.js web interface.

## Local development

Use Python 3.12, Node.js 22, and pnpm. Create a virtual environment with `python -m venv .venv`.
Activate it with `.venv\Scripts\Activate.ps1` on Windows or `source .venv/bin/activate` on macOS or Linux.

```sh
python -m pip install -r requirements.txt
pnpm install --frozen-lockfile
```

Copy `.env.example` to `.env.local`. Set the Cal.com API key for an isolated test account, a Gemini API key from an unbilled project, and a signing secret generated with `python -c "import secrets; print(secrets.token_hex(32))"`. The test account needs availability and a visible 30-minute event type.

Run `pnpm dev:api` and `pnpm dev` in separate terminals, then open `http://localhost:3000`. Run the checks from the repository root.

```sh
pnpm test
pnpm lint
```

## Known limitations

- The browser-held session token is readable and includes booking IDs and the user's email. HMAC verifies the token's integrity. Use an isolated test account.
- Anyone who knows an attendee's email can look up that attendee's bookings in the demo.
- Google can use free-tier Gemini prompts and responses to improve its products. Use fictional contact details in conversation and keep deliverable test aliases in the local Cal.com probe settings.
