import os
import re
import logging
import pypdf
import pytesseract
from pdf2image import convert_from_path
from llama_index.core import Document

logger = logging.getLogger(__name__)


def pdf_text(filepath: str) -> str:
    reader = pypdf.PdfReader(filepath)
    text = ""
    for page in reader.pages:
        text += page.extract_text() or ""
    return text.strip()


def pdf_text_ocr(filepath: str) -> str:
    logger.info(f"Running OCR on {os.path.basename(filepath)}...")
    try:
        images = convert_from_path(filepath, dpi=200)
        text = ""
        for image in images:
            text += pytesseract.image_to_string(image) + "\n"
        return text.strip()
    except Exception as e:
        logger.error(f"OCR failed for {os.path.basename(filepath)}: {e}", exc_info=True)
        return ""


def clean_text(text: str) -> str:
    # Remove table of contents dot leaders
    text = re.sub(r'\.{4,}', '', text)

    # Remove page number lines
    text = re.sub(r'(?i)^page\s+\d+.*$', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*\d+\s*$', '', text, flags=re.MULTILINE)

    # Remove date/reference number patterns
    text = re.sub(r'\d{2}/\d{2}/\d{4}\s+[\d\-]+', '', text)
    text = re.sub(r'\b\d{4,}-\d{3,}\b', '', text)

    # Clean up whitespace
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r' {2,}', ' ', text)

    return text.strip()


def load_pdf(filepath: str, metadata: dict) -> list:
    filename = os.path.basename(filepath)

    try:
        text = pdf_text(filepath)
    except Exception as e:
        logger.error(f"pypdf failed for {filename}: {e}", exc_info=True)
        text = ""

    if len(text) < 100:
        logger.info(f"pypdf returned little/no text for {filename}, trying OCR...")
        text = pdf_text_ocr(filepath)

    if not text:
        logger.warning(f"Could not extract any text from {filename}, skipping.")
        return []

    text = clean_text(text)

    chunk_size = 256
    overlap = 50
    chunks = []
    start = 0

    while start < len(text):
        end = start + chunk_size
        chunk_text = text[start:end]

        # Each chunk gets its own metadata copy 
        chunks.append(Document(text=chunk_text, metadata=dict(metadata)))
        start = end - overlap

    logger.info(f"Extracted {len(chunks)} chunks from {filename}")
    return chunks
