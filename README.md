# Christmas Categories

Scan a book's ISBN barcode with your phone and see **Title by Author** plus the
Christmas category and shelf it belongs on. Nothing is saved.

| Category | Shelf |
|---|---|
| Baby Christmas | Shelf 1aBc |
| Toddler Christmas | Shelf 1bTc |
| Children's Christmas | Shelf 1cCc |
| Y/A Christmas | Shelf 2aYc |
| Christmas Romance | Shelf 3aRc |
| Christmas Fiction | Shelf 4aFc |
| Christmas Mystery | Shelf 5aMc |
| Christmas History | Shelf 6aHc |
| Christmas Classics | Shelf 7aCc |
| Christmas Creations | Shelf 8aCc |
| Needs Review | Hold / Check Cover |

## Files
- `app.py` – Flask server: ISBN lookup (Google Books + Open Library), rules, Claude classifier
- `templates/index.html` – the scanner page
- `Procfile`, `requirements.txt`, `runtime.txt` – deployment

## Environment variables
- `ANTHROPIC_API_KEY` (required for AI sorting; without it the app uses simple keyword rules)
- `GOOGLE_BOOKS_API_KEY` (optional, avoids Google rate limits)
- `CLAUDE_MODEL` (optional, defaults to `claude-opus-5-5`)

## Run locally
```
pip install -r requirements.txt
python app.py
```
Camera scanning needs HTTPS, so test the camera on the deployed site.
