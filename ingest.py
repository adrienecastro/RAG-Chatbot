import os
import hmac
import json
import hashlib
import logging
import chromadb
import tqdm.auto as tqdm_auto
from tqdm import tqdm as tqdm_module
from dotenv import load_dotenv
from gdrive_sync import get_gdrive_urls
from pdf_reader import load_pdf

# Must be set before llama_index imports
load_dotenv()
os.environ["LLAMA_INDEX_CACHE_DIR"] = os.getenv("LLAMA_INDEX_CACHE_DIR")
os.environ["HF_HOME"] = os.getenv("HF_HOME")
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_DATASETS_OFFLINE"] = "1"

from llama_index.core import VectorStoreIndex, StorageContext, Settings, Document
from llama_index.core.node_parser import SentenceSplitter
from llama_index.vector_stores.chroma import ChromaVectorStore
from llama_index.embeddings.huggingface import HuggingFaceEmbedding

# Ignores noisy pdf warnings
logging.getLogger("pypdf").setLevel(logging.ERROR)
logger = logging.getLogger(__name__)


Settings.llm = None
Settings.embed_model = HuggingFaceEmbedding(model_name=os.getenv("EMBEDDING_MODEL_PATH"), embed_batch_size=64)
Settings.transformations = [SentenceSplitter(chunk_size=256, chunk_overlap=50)]


# Overrides tqdm defaults for a prettier progress bar ^_^
class customTqdm(tqdm_module):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("bar_format", "  Embedding [{bar:40}] {percentage:3.0f}% ({n_fmt}/{total_fmt} chunks) (Time Elapsed: {elapsed}, Remaining: {remaining})")
        kwargs.setdefault("colour", None)
        kwargs.setdefault("ascii", "-#")
        super().__init__(*args, **kwargs)

tqdm_auto.tqdm = customTqdm


# Variables
HASH_CACHE_PATH = "/opt/KeyWatchBot/.hash-cache.json"
CACHE_SECRET = os.getenv("HASH_CACHE_SECRET")
EXTENSIONS = {".pdf", ".txt"}

# Max pdf size to process in mb
MAX_PDF_SIZE = 50    

# Keys to strip before indexing
EXCLUDED_METADATA_KEYS = {
    "chunk_start", "chunk_end", "file_size", "file_type",
    "creation_date", "last_modified_date", "last_accessed_date",
    "page_label", "source", "txt_keywords"
}


# Security functions
def max_pdf_size(filepath: str) -> bool:
    pdf_size = os.path.getsize(filepath) / (1024 * 1024)
    if pdf_size > MAX_PDF_SIZE:
        logger.warning(f"Skipping oversized file ({pdf_size:.1f}MB): {os.path.basename(filepath)}")
        return True
    return False


def get_file_hash(filepath: str) -> str:
    with open(filepath, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


# Returns SHA256 hash of cache contents
def sign_cache(cache: dict) -> str:
    if not CACHE_SECRET:
        return ""
    content = json.dumps(cache, sort_keys=True)
    return hmac.new(CACHE_SECRET.encode(), content.encode(), "sha256").hexdigest()


def load_hash_cache(cache_path: str) -> dict:
    if not os.path.exists(cache_path):
        return {}
    try:
        with open(cache_path) as f:
            content = f.read().strip()
            if not content:
                return {}
            data = json.loads(content)

        # Verify hash if secret is configured
        if CACHE_SECRET:
            stored_sig = data.pop("__signature__", "")
            expected_sig = sign_cache(data)
            if not hmac.compare_digest(stored_sig, expected_sig):
                logger.warning("Hash cache signature mismatch — cache may have been tampered with. Starting fresh.")
                return {}
        return data

    except json.JSONDecodeError:
        logger.warning("Hash cache corrupted, starting fresh.")
        return {}


def save_hash_cache(cache_path: str, cache: dict):
    data = dict(cache)
    if CACHE_SECRET:
        data["__signature__"] = sign_cache(cache)
    with open(cache_path, "w") as f:
        json.dump(data, f, indent=2)


# Parse txt file headers
def get_txt_headers(text: str) -> tuple:
    HEADER_KEYS = {
        "TOPIC", "PRODUCT", "CATEGORY",
        "KEYWORDS", "SOURCE_PDF", "SOURCE_URL"
    }
    lines = text.splitlines()
    headers = {}
    content_start = 0

    for i, line in enumerate(lines):
        stripped = line.strip()
        if ":" in stripped:
            key = stripped.split(":", 1)[0].strip().upper()
            if key in HEADER_KEYS:
                value = stripped.split(":", 1)[1].strip()
                headers[key] = value
                content_start = i + 1
                continue
        break

    content = "\n".join(lines[content_start:]).strip()
    return content, headers


# Clears unused metadata
def clear_metadata(docs: list) -> list:
    for doc in docs:
        for key in EXCLUDED_METADATA_KEYS:
            doc.metadata.pop(key, None)
    return docs


# Main Ingest
def run_ingest() -> str:
    try:
        hash_cache = load_hash_cache(HASH_CACHE_PATH)
        new_hash_cache = {}

        print("Fetching PDF links from Google Drive...")
        drive_links = get_gdrive_urls()
        print(f"Found {len(drive_links)} PDFs across all Drive folders.")

        raw_paths = os.getenv("DOCS_PATHS")
        docs_paths = [p.strip() for p in raw_paths.split(",") if p.strip()]

        if not docs_paths:
            return "Ingestion failed: `DOCS_PATHS is not set in .env`"

        all_docs = []
        skipped = 0
        failed = 0

        for path in docs_paths:
            if not os.path.exists(path):
                logger.warning(f"Path '{path}' does not exist, skipping.")
                continue

            category = os.path.basename(path.rstrip("/"))
            print(f"\nProcessing category: '{category}'")

            for root, _, files in os.walk(path):
                for filename in files:
                    ext = os.path.splitext(filename)[1].lower()
                    if ext not in EXTENSIONS:
                        continue

                    filepath = os.path.join(root, filename)
                    parent_folder = os.path.basename(root)
                    product = ( parent_folder if parent_folder != category else None)

                    # Ignore oversized pdfs
                    if ext == ".pdf" and max_pdf_size(filepath):
                        failed += 1
                        continue

                    try:
                        file_hash = get_file_hash(filepath)
                    except Exception as e:
                        logger.error(f"Could not hash {filename}: {e}",exc_info=True)
                        failed += 1
                        continue

                    new_hash_cache[filepath] = file_hash

                    if hash_cache.get(filepath) == file_hash:
                        print(f"Skipping unchanged: {filename}")
                        skipped += 1
                        continue

                    # Start txt file ingest
                    if ext == ".txt":
                        try:
                            with open(filepath, "r", encoding="utf-8") as f:
                                raw_text = f.read().strip()

                            if not raw_text:
                                continue

                            content, headers = get_txt_headers(raw_text)

                            display_name = headers.get("SOURCE_PDF", filename)
                            drive_url = (headers.get("SOURCE_URL") or drive_links.get(display_name))
                            txt_product = product or headers.get("PRODUCT")

                            keyword_list = [k.strip().lower() for k in headers.get("KEYWORDS").split(",") if k.strip()]
                            keywords = ",".join(keyword_list)

                            metadata = {
                                "display_name": display_name,
                                "file_path": os.path.abspath(filepath),
                                "drive_url": drive_url,
                                "category": category,
                                "product": txt_product,
                                "image_heavy": "False",
                                "source_type": "txt",
                                "txt_keywords": keywords
                            }

                            chunk_size = 1536
                            overlap = 50
                            chunks = []
                            start = 0
                            while start < len(content):
                                chunk_text = content[start:start + chunk_size]
                                chunks.append(Document(text=chunk_text, metadata=dict(metadata)))
                                start += chunk_size - overlap

                            all_docs.extend(chunks)
                            print(f"Loaded {len(chunks)} chunks from {filename} ({len(keyword_list)} keywords) (source: {display_name})")

                        except Exception as e:
                            logger.error(f"Error processing {filename}: {e}", exc_info=True)
                            failed += 1
                        continue

                    # Resume pdf ingest
                    metadata = {
                        "display_name": filename,
                        "file_path": os.path.abspath(filepath),
                        "drive_url": drive_links.get(filename),
                        "category": category,
                        "product": product,
                        "image_heavy": "False"
                    }

                    try:
                        chunks = load_pdf(filepath, metadata)

                        if chunks:
                            total_text = " ".join([c.text for c in chunks])
                            image_heavy_pdf = len(total_text) < 500
                            for chunk in chunks:
                                chunk.metadata["image_heavy"] = str(image_heavy_pdf)

                            all_docs.extend(chunks)
                            print(f"Loaded {len(chunks)} chunks from {filename}" + (" [image-heavy]" if image_heavy_pdf else "") + (f" [{product}]" if product else ""))
                        else:
                            logger.warning(f"No text extracted from {filename}, skipping.")
                            failed += 1

                    except Exception as e:
                        logger.error(f"Error processing {filename}: {e}", exc_info=True)
                        failed += 1
                        continue

        print(f"\nSummary: {len(all_docs)} chunks to index, {skipped} files skipped (unchanged), {failed} files failed.")

        if not all_docs:
            save_hash_cache(HASH_CACHE_PATH, new_hash_cache)
            return (f"No new or changed files found. Index is up to date. ({skipped} files unchanged, {failed} failed)")

        all_docs = clear_metadata(all_docs)

        print("Building vector index...")
        db = chromadb.PersistentClient(path=os.getenv("CHROMA_PATH"))

        # Deletes database if it already exists
        existing = [c.name for c in db.list_collections()]
        if "support_docs" in existing:
            db.delete_collection("support_docs")

        collection = db.get_or_create_collection("support_docs")
        vector_store = ChromaVectorStore(chroma_collection=collection)
        storage_context = StorageContext.from_defaults(vector_store=vector_store)
        VectorStoreIndex.from_documents(all_docs, storage_context=storage_context, show_progress=True)

        save_hash_cache(HASH_CACHE_PATH, new_hash_cache)
        print(f"Index now contains {collection.count()} chunks.")

        return (f"Re-indexed successfully. Loaded {len(all_docs)} chunks from {len(docs_paths)} folders ({skipped} files unchanged, {failed} files failed, {len(drive_links)} google drive files found).")

    except Exception as e:
        logger.error(f"Ingestion failed: {e}", exc_info=True)
        return f"Ingestion failed: `{str(e)}`"


# Ingest entry point
if __name__ == "__main__":
    print(run_ingest())