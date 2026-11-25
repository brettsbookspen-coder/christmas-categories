"""
Christmas Categories - scan a book's ISBN barcode and find out which
Christmas shelf it belongs on.

Flow:
  1. Phone camera scans ISBN-13 barcode (in index.html).
  2. /api/classify looks the ISBN up (Google Books, then Open Library).
  3. Quick rules handle the obvious cases (famous classics, no Christmas
     connection at all).
  4. Everything else goes to Claude with the shelf-sorting prompt.
  5. The shelf code always comes from the SHELVES table below, so it can
     never be misspelled.

Nothing is saved. No database, no inventory.
"""

import os
import re
from typing import Literal

import anthropic
import requests
from flask import Flask, jsonify, render_template, request
from pydantic import BaseModel

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Categories -> shelf codes (Bin Number = Shelf Name)
# ---------------------------------------------------------------------------
SHELVES = {
    "Baby Christmas": "Shelf 1aBc",
    "Toddler Christmas": "Shelf 1bTc",
    "Children's Christmas": "Shelf 1cCc",
    "Y/A Christmas": "Shelf 2aYc",
    "Christmas Romance": "Shelf 3aRc",
    "Christmas Fiction": "Shelf 4aFc",
    "Christmas Mystery": "Shelf 5aMc",
    "Christmas History": "Shelf 6aHc",
    "Christmas Classics": "Shelf 7aCc",
    "Christmas Creations": "Shelf 8aCc",
}
NEEDS_REVIEW = "Needs Review"
HOLD_SHELF = "Hold / Check Cover"

CategoryName = Literal[
    "Baby Christmas",
    "Toddler Christmas",
    "Children's Christmas",
    "Y/A Christmas",
    "Christmas Romance",
    "Christmas Fiction",
    "Christmas Mystery",
    "Christmas History",
    "Christmas Classics",
    "Christmas Creations",
    "Needs Review",
]

# Universally recognized classics -> always "Christmas Classics"
CLASSIC_TITLES = [
    "a christmas carol",
    "the nutcracker",
    "polar express",
    "how the grinch stole christmas",
    "the night before christmas",
    "twas the night before christmas",
    "'twas the night before christmas",
    "a visit from st. nicholas",
    "a visit from saint nicholas",
    "the gift of the magi",
    "yes, virginia",
    "the little match girl",
    "rudolph the red-nosed reindeer",
    "the best christmas pageant ever",
    "a charlie brown christmas",
    "miracle on 34th street",
    "the snowman",  # Raymond Briggs
    "a child's christmas in wales",
    "the year without a santa claus",
    "the story of holly and ivy",
    "the father christmas letters",
    "letters from father christmas",
    "the christmas story",  # Luke 2 retellings
    "the little drummer boy",
    "frosty the snowman",
    "the elves and the shoemaker",
    "the fir tree",
]

CHRISTMAS_WORDS = re.compile(
    r"christmas|xmas|santa|st\.? nick|saint nicholas|nativity|advent|yule|"
    r"noel|nutcracker|reindeer|rudolph|grinch|mistletoe|north pole|elf on|"
    r"elves|jingle|sleigh|stocking|december 25|holiday",
    re.IGNORECASE,
)
WINTER_WORDS = re.compile(r"winter|snow|frost|ice|cold|blizzard|mitten", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Book lookup
# ---------------------------------------------------------------------------
HTTP_TIMEOUT = 8


def clean_isbn(raw: str) -> str:
    return re.sub(r"[^0-9Xx]", "", raw or "").upper()


def lookup_google_books(isbn: str) -> dict | None:
    params = {"q": f"isbn:{isbn}"}
    key = os.environ.get("GOOGLE_BOOKS_API_KEY")
    if key:
        params["key"] = key
    r = requests.get(
        "https://www.googleapis.com/books/v1/volumes", params=params, timeout=HTTP_TIMEOUT
    )
    r.raise_for_status()
    items = r.json().get("items") or []
    if not items:
        return None
    info = items[0].get("volumeInfo", {})
    return {
        "title": info.get("title", ""),
        "subtitle": info.get("subtitle", ""),
        "authors": info.get("authors", []),
        "description": info.get("description", ""),
        "categories": info.get("categories", []),
        "pageCount": info.get("pageCount") or "",
        "printType": info.get("printType", ""),
        "maturityRating": info.get("maturityRating", ""),
        "publishedDate": info.get("publishedDate", ""),
        "cover": (info.get("imageLinks") or {}).get("thumbnail", "").replace("http://", "https://"),
    }


def lookup_open_library(isbn: str) -> dict | None:
    r = requests.get(
        "https://openlibrary.org/api/books",
        params={"bibkeys": f"ISBN:{isbn}", "format": "json", "jscmd": "data"},
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    data = r.json().get(f"ISBN:{isbn}")
    if not data:
        return None
    desc = ""
    # "data" view has no description; try the edition record for one + format
    fmt = ""
    try:
        ed = requests.get(f"https://openlibrary.org/isbn/{isbn}.json", timeout=HTTP_TIMEOUT).json()
        d = ed.get("description", "")
        desc = d.get("value", "") if isinstance(d, dict) else d
        fmt = ed.get("physical_format", "")
    except Exception:
        pass
    return {
        "title": data.get("title", ""),
        "subtitle": data.get("subtitle", ""),
        "authors": [a.get("name", "") for a in data.get("authors", [])],
        "description": desc,
        "categories": [s.get("name", "") for s in data.get("subjects", [])][:25],
        "pageCount": data.get("number_of_pages") or "",
        "printType": fmt or "BOOK",
        "maturityRating": "",
        "publishedDate": data.get("publish_date", ""),
        "cover": (data.get("cover") or {}).get("medium", ""),
    }


def lookup_book(isbn: str) -> dict | None:
    google = None
    try:
        google = lookup_google_books(isbn)
    except Exception as e:
        app.logger.warning("Google Books lookup failed: %s", e)
    openlib = None
    # Open Library is a fallback, and also fills in gaps (subjects, format)
    if not google or not google.get("description") or not google.get("categories"):
        try:
            openlib = lookup_open_library(isbn)
        except Exception as e:
            app.logger.warning("Open Library lookup failed: %s", e)
    if not google:
        return openlib
    if openlib:
        for k in ("description", "subtitle", "pageCount", "cover"):
            if not google.get(k) and openlib.get(k):
                google[k] = openlib[k]
        google["categories"] = list(dict.fromkeys(google["categories"] + openlib["categories"]))
        if openlib.get("printType") and openlib["printType"] != "BOOK":
            google["printType"] = f'{google["printType"]} / {openlib["printType"]}'
    return google


# ---------------------------------------------------------------------------
# Rules (run before the LLM)
# ---------------------------------------------------------------------------
def all_text(book: dict) -> str:
    return " ".join(
        [
            book.get("title", ""),
            book.get("subtitle", ""),
            book.get("description", ""),
            " ".join(book.get("categories", [])),
        ]
    )


def rule_classify(book: dict) -> dict | None:
    """Return a result for obvious cases, or None to let the LLM decide."""
    title = f'{book.get("title", "")} {book.get("subtitle", "")}'.lower().replace("’", "'")
    for classic in CLASSIC_TITLES:
        if classic in title:
            return {
                "category": "Christmas Classics",
                "reason": "A universally recognized Christmas classic.",
            }
    text = all_text(book)
    if not CHRISTMAS_WORDS.search(text):
        if WINTER_WORDS.search(text):
            reason = "Winter/snow themed with no clear Christmas connection."
        else:
            reason = "No Christmas or holiday connection found in the book's info."
        return {"category": NEEDS_REVIEW, "reason": reason}
    return None


def rule_fallback(book: dict) -> dict:
    """Used only when the LLM is unavailable (no API key / API error)."""
    text = all_text(book).lower()
    pages = book.get("pageCount") or 0
    try:
        pages = int(pages)
    except (TypeError, ValueError):
        pages = 0
    if "board book" in text or "board books" in text or "board_book" in text:
        cat = "Baby Christmas"
    elif any(w in text for w in ["cookbook", "cooking", "baking", "recipes", "crafts", "decorating", "diy"]):
        cat = "Christmas Creations"
    elif any(w in text for w in ["young adult", "teen"]):
        cat = "Y/A Christmas"
    elif "juvenile" in text or "picture book" in text:
        cat = "Toddler Christmas" if 0 < pages <= 48 else "Children's Christmas"
    elif "romance" in text or "love story" in text:
        cat = "Christmas Romance"
    elif any(w in text for w in ["mystery", "thriller", "detective", "suspense", "whodunit"]):
        cat = "Christmas Mystery"
    elif any(w in text for w in ["history", "traditions", "folklore", "social science", "music"]):
        cat = "Christmas History"
    elif "fiction" in text:
        cat = "Christmas Fiction"
    else:
        return {"category": NEEDS_REVIEW, "reason": "Not enough information to choose a shelf."}
    return {"category": cat, "reason": "Sorted by keyword rules (AI unavailable) - double-check."}


# ---------------------------------------------------------------------------
# LLM classification (Claude)
# ---------------------------------------------------------------------------
PROMPT = """You are a bookstore shelf-sorting assistant. Given a book's metadata, classify it into EXACTLY ONE of the 10 Christmas categories below, or "Needs Review" if ambiguous.
CATEGORIES & SHELF CODES:

1. "Baby Christmas" -> "Shelf 1aBc" (Board books for babies/infants ages 0-2)
2. "Toddler Christmas" -> "Shelf 1bTc" (Illustrated picture books for young children ages 2-5)
3. "Children's Christmas" -> "Shelf 1cCc" (Chapter books and middle-grade novels for kids ages 6-12)
4. "Y/A Christmas" -> "Shelf 2aYc" (Young Adult / Teen fiction ages 13-18)
5. "Christmas Romance" -> "Shelf 3aRc" (Adult romance and holiday love stories)
6. "Christmas Fiction" -> "Shelf 4aFc" (Adult general, literary, or family Christmas fiction not primarily romance or mystery)
7. "Christmas Mystery" -> "Shelf 5aMc" (Christmas mysteries, cozy whodunits, suspense, or thrillers)
8. "Christmas History" -> "Shelf 6aHc" (Non-fiction about Christmas history, traditions, folklore, music, or culture)
9. "Christmas Classics" -> "Shelf 7aCc" (Famous classic Christmas stories such as A Christmas Carol, The Nutcracker, The Polar Express, or vintage holiday staples)
10. "Christmas Creations" -> "Shelf 8aCc" (Christmas cookbooks, baking, crafts, decorating, or DIY)

STRICT RULES:

* Return "Needs Review" (shelf: "Hold / Check Cover") if:
a) The book is merely "winter" or "snow" themed with no clear Christmas/holiday connection.
b) The metadata is too sparse to distinguish age group (e.g., Picture Book vs. Chapter Book) or genre.
c) It is a multi-genre anthology or non-Christmas holiday book that doesn't clearly belong on the Christmas display.
* Prioritize Classics ("Christmas Classics") over age/genre if the title is a universally recognized Christmas classic.
* Use clues like "Board book", page count (<32 pages often indicates Toddler/Baby), and BISAC codes (e.g., JUV017010 = Juvenile Christmas) to separate Baby, Toddler, and Children's.

BOOK METADATA:
Title: {title}
Subtitle: {subtitle}
Authors: {authors}
Description: {description}
Subjects/BISAC: {categories}
Page Count / Format: {pageCount} / {printType}
Maturity Rating: {maturityRating}
Published: {publishedDate}

Respond with JSON:
{{
"title": "Clean Book Title",
"author": "Primary Author",
"category": "Exact Main Name or Needs Review",
"shelf": "Exact Shelf Code or Hold / Check Cover",
"reason": "One short sentence explaining why"
}}"""


class Classification(BaseModel):
    title: str
    author: str
    category: CategoryName
    shelf: str
    reason: str


_client = None


def get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def llm_classify(book: dict) -> dict:
    prompt = PROMPT.format(
        title=book.get("title") or "(unknown)",
        subtitle=book.get("subtitle") or "(none)",
        authors=", ".join(book.get("authors") or []) or "(unknown)",
        description=(book.get("description") or "(none)")[:4000],
        categories="; ".join(book.get("categories") or []) or "(none)",
        pageCount=book.get("pageCount") or "(unknown)",
        printType=book.get("printType") or "(unknown)",
        maturityRating=book.get("maturityRating") or "(unknown)",
        publishedDate=book.get("publishedDate") or "(unknown)",
    )
    response = get_client().beta.messages.parse(
        model=os.environ.get("CLAUDE_MODEL", "claude-opus-5-5"),
        max_tokens=4000,
        output_config={"effort": "low"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_format=Classification,
        messages=[{"role": "user", "content": prompt}],
    )
    if response.stop_reason == "refusal" or response.parsed_output is None:
        raise RuntimeError(f"No classification returned (stop_reason={response.stop_reason})")
    out = response.parsed_output
    return {"title": out.title, "author": out.author, "category": out.category, "reason": out.reason}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/classify", methods=["POST"])
def classify():
    isbn = clean_isbn((request.get_json(silent=True) or {}).get("isbn", ""))
    if len(isbn) not in (10, 13):
        return jsonify({"error": "That doesn't look like an ISBN. Try scanning again."}), 400

    book = lookup_book(isbn)
    if not book or not book.get("title"):
        return jsonify(
            {
                "isbn": isbn,
                "title": "Unknown Book",
                "author": "Unknown Author",
                "category": NEEDS_REVIEW,
                "shelf": HOLD_SHELF,
                "reason": "No book info found for this ISBN. Check the cover.",
            }
        )

    title = book["title"]
    author = (book.get("authors") or ["Unknown Author"])[0]

    result = rule_classify(book)
    source = "rules"
    if result is None:
        if os.environ.get("ANTHROPIC_API_KEY"):
            try:
                result = llm_classify(book)
                source = "ai"
                # keep clean title/author from the AI if it gave one
                title = result.get("title") or title
                author = result.get("author") or author
            except Exception as e:
                app.logger.exception("Claude classification failed: %s", e)
                result = rule_fallback(book)
        else:
            result = rule_fallback(book)

    category = result["category"].strip()
    shelf = SHELVES.get(category, HOLD_SHELF)
    if category not in SHELVES:
        category = NEEDS_REVIEW

    return jsonify(
        {
            "isbn": isbn,
            "title": title,
            "author": author,
            "category": category,
            "shelf": shelf,
            "reason": result.get("reason", ""),
            "cover": book.get("cover", ""),
            "source": source,
        }
    )


@app.route("/health")
def health():
    return "ok"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
