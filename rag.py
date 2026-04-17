import os
import time
import logging
import chromadb
import urllib.parse
from dotenv import load_dotenv
from google import genai
from google.genai import types
from google.genai.errors import ServerError, ClientError

# Must be set before any llama_index imports
load_dotenv()
os.environ["LLAMA_INDEX_CACHE_DIR"] = os.getenv("LLAMA_INDEX_CACHE_DIR")
os.environ["HF_HOME"] = os.getenv("HF_HOME")
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_DATASETS_OFFLINE"] = "1"

from llama_index.core import VectorStoreIndex, StorageContext, Settings
from llama_index.core.vector_stores import ExactMatchFilter, MetadataFilters
from llama_index.core.schema import MetadataMode
from llama_index.vector_stores.chroma import ChromaVectorStore
from llama_index.embeddings.huggingface import HuggingFaceEmbedding

logger = logging.getLogger(__name__)


Settings.embed_model = HuggingFaceEmbedding(model_name=os.getenv("EMBEDDING_MODEL_PATH", "sentence-transformers/all-MiniLM-L6-v2"), embed_batch_size=64)
gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))


# Class to catch any gemini server errors
class geminiUnavailable(Exception):
    pass


# Variables
MIN_SOURCE_SCORE = 0.3
CONFIDENCE_THRESHOLD = 0.25

# Minimum number of keywords needed to pull text file chunks
TXT_KEYWORD_MIN = 3

# Metadata keys that shouldn't get passed to the llm
EXCLUDED_LLM_KEYS = [
    "chunk_start", "chunk_end", "file_path", "file_size", "file_type",
    "creation_date", "last_modified_date", "last_accessed_date",
    "page_label", "source", "image_heavy", "source_type", "txt_keywords"
]

TXT_FILTER = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been",
    "being", "have", "has", "had", "do", "does", "did", "will",
    "would", "could", "should", "may", "might", "can", "shall",
    "to", "of", "in", "for", "on", "with", "at", "by", "from",
    "up", "about", "into", "through", "how", "what", "when",
    "where", "why", "who", "which", "this", "that", "these",
    "those", "i", "my", "we", "our", "you", "your", "it", "its",
    "not", "no", "and", "or", "but", "if", "then", "so", "as"
}


# System prompts
SYSTEM_PROMPT = """You are a helpful internal IT support assistant.
Your job is to answer support questions clearly and concisely based only on the provided context.

Strict rules you must always follow:
- Answer only using information from the provided context
- Your response must contain ONLY the answer to the question and nothing else
- Never include any of the following in your response under any circumstances:
  * Metadata of any kind
  * File paths, file names, or folder names
  * Dates, timestamps, or reference numbers
  * Chunk numbers, offsets, or any internal document structure
  * Technical document properties of any kind
  * The words chunk_start, chunk_end, file_path, source, or page_label
- If the answer has multiple steps, format them as a numbered list
- If the answer is a single fact or explanation, respond in plain prose
- If the context does not contain enough information to answer the question,
  say exactly: "I don't have enough information in my knowledge base to
  answer that. Please contact a senior support agent."
- Keep responses concise and actionable — users are non-IT who need
  quick, clear answers
- Never make up information that is not in the context
"""

FALLBACK_PROMPT = SYSTEM_PROMPT + """
Since no relevant internal documentation was found, you may use general IT
and networking knowledge to answer this question.

Additional strict rules for this response:
- Never suggest contacting a vendor, manufacturer, or external support
- Never suggest contacting IT support or any external party — you ARE
  the support team
- Never suggest purchasing new hardware or software
- Never reference external documentation, websites, or resources
- If the answer requires company-specific information you don't have,
  say: "I don't have enough information in my knowledge base to answer
  that. Please contact a senior support agent."
- At the end of your response always add on a new line:
  "Note: This answer is based on general IT knowledge, not internal
  documentation."
"""


# Checks whether the user's question contains enough matching keywords for txt node retrieval 
def txt_node_filter(question: str, node_metadata: dict, threshold: int = TXT_KEYWORD_MIN) -> tuple:
    source_type = node_metadata.get("source_type")
    if source_type != "txt":
        return True, 0

    stored = node_metadata.get("txt_keywords")
    if not stored:
        return True, 0

    file_keywords = set(stored.split(","))

    # Extract meaningful words from the question
    question_words = {w.strip("?.,!:;'\"").lower() for w in question.split() if w.strip("?.,!:;'\"").lower() not in TXT_FILTER and len(w.strip("?.,!:;'\"")) > 2}

    # Count how many question words appear in the file's keyword list
    matches = question_words & file_keywords
    match_count = len(matches)

    passes = match_count >= threshold
    return passes, match_count


# Index loader
def load_index():
    db = chromadb.PersistentClient(path=os.getenv("CHROMA_PATH"))
    collection = db.get_or_create_collection("support_docs")
    vector_store = ChromaVectorStore(chroma_collection=collection)
    storage_context = StorageContext.from_defaults(vector_store=vector_store)
    return VectorStoreIndex.from_vector_store(vector_store, storage_context=storage_context)


# If retrieved nodes span multiple products, keep only chunks from the single highest-scoring product
def deduplicate_products(nodes: list) -> list:
    product_nodes = [n for n in nodes if n.metadata.get("product")]
    general_nodes = [n for n in nodes if not n.metadata.get("product")]

    if not product_nodes:
        return nodes

    product_scores = {}
    for node in product_nodes:
        product = node.metadata.get("product")
        score = node.score or 0
        if product not in product_scores or score > product_scores[product]:
            product_scores[product] = score

    best_product = max(product_scores, key=product_scores.get)
    filtered = [n for n in product_nodes if n.metadata.get("product") == best_product]

    return general_nodes + filtered


# Gemini call & response builder
def gemini_call(model: str, prompt: str, system: str, contents: list, max_retries: int) -> str:
    gemini_error = None

    for attempt in range(max_retries + 1):
        try:
            response = gemini_client.models.generate_content(model=model, contents=contents, config=types.GenerateContentConfig(system_instruction=system, max_output_tokens=8192, temperature=0.2))
            answer = response.text.strip()

            # Bot grammar check
            last_char = answer[-1] if answer else ""
            seems_truncated = (last_char not in ".!?:" and not answer.endswith("agent.") and attempt < max_retries)

            if seems_truncated:
                logger.warning(f"[{model}] Response appears truncated (attempt {attempt + 1}), retrying...")

                # Add a continuation step
                contents = contents + [types.Content(role="model", parts=[types.Part(text=answer)]),
                types.Content(role="user", parts=[types.Part(text=("Please continue from where you left off and complete the answer - do not cut off midstep."))])]
                continue
            return answer

        except ServerError as e:
            gemini_error = e
            if attempt < max_retries:
                wait = 2 ** attempt
                logger.warning(f"[{model}] Server error (attempt {attempt + 1}/{max_retries + 1}), retrying in {wait}s: {e}")
                time.sleep(wait)

        except ClientError:
            raise

        except Exception as e:
            gemini_error = e
            if attempt < max_retries:
                wait = 2 ** attempt
                logger.warning(f"[{model}] Unexpected error (attempt {attempt + 1}), retrying in {wait}s: {e}")
                time.sleep(wait)

    raise geminiUnavailable(f"[{model}] Unavailable after {max_retries + 1} attempts: {gemini_error}")


# Builds the query context to send to gemini_call() - also tries a secondary gemini model if the primary fails
def gemini_query(prompt: str, system: str, history: list = None, max_retries: int = 3) -> str:
    primary_model = os.getenv("GEMINI_PRIMARY_MODEL")
    secondary_model = os.getenv("GEMINI_SECONDARY_MODEL")

    # Build the contents list from history + current prompt
    contents = []
    if history:
        for entry in history:
            role = "model" if entry["role"] == "assistant" else "user"
            contents.append(types.Content(role=role, parts=[types.Part(text=entry["content"])]))
    contents.append(types.Content(role="user", parts=[types.Part(text=prompt)]))

    # Try primary model
    try:
        logger.info(f"Calling primary model: {primary_model}")
        return gemini_call(primary_model, prompt, system, contents, max_retries)

    except geminiUnavailable as primary_error:
        logger.warning(f"Primary model ({primary_model}) failed, switching to secondary ({secondary_model}): {primary_error}")

    except ClientError as e:
        logger.error(f"Gemini client error: {e}", exc_info=True)
        raise geminiUnavailable(f"Gemini client error: {e}")

    # Try secondary model
    try:
        logger.info(f"Calling secondary model: {secondary_model}")
        return gemini_call(secondary_model, prompt, system, contents, max_retries)

    except geminiUnavailable as secondary_error:
        logger.error(f"Fallback model ({secondary_model}) also failed: {secondary_error}", exc_info=True)
        raise geminiUnavailable(f"Both primary ({primary_model}) and secondary ({secondary_model}) models are unavailable.")


# Main query
def query(question: str, product: str = None, history: list = None, feedback_hints: str = "") -> dict:
    empty_result = {
        "answer": "",
        "sources": {},
        "categories": {},
        "source_paths": {},
        "source_scores": {},
        "image_heavy_sources": {}
    }

    try:
        index = load_index()

        if product:
            filters = MetadataFilters(filters=[ExactMatchFilter(key="product", value=product)])
            retriever = index.as_retriever(similarity_top_k=6, filters=filters)

        else:
            retriever = index.as_retriever(similarity_top_k=6)

        nodes = retriever.retrieve(question)
        nodes = deduplicate_products(nodes)

        # Drop nodes that are below a minimum relevance score
        nodes = [n for n in nodes if (n.score or 0) >= MIN_SOURCE_SCORE]

        # TXT file keyword filter
        filtered_nodes = []
        for node in nodes:
            source_type = node.metadata.get("source_type", "")

            if source_type == "txt":
                passes, match_count = txt_node_filter(question, node.metadata)

                if passes:
                    filtered_nodes.append(node)
                else:
                    source_name = node.metadata.get("display_name", "unknown")
                    logger.info(f"TXT filter dropped '{source_name}' ({match_count} keyword matches, threshold is {TXT_KEYWORD_MIN})")
            else:
                filtered_nodes.append(node)

        nodes = filtered_nodes

        # Retrieve "high confidence" nodes
        high_confidence_nodes = [n for n in nodes if (n.score or 0) >= CONFIDENCE_THRESHOLD]
        image_heavy_nodes = [n for n in high_confidence_nodes if n.metadata.get("image_heavy", "False") == "True"]
        text_nodes = [n for n in high_confidence_nodes if n.metadata.get("image_heavy", "False") != "True"]

        # Collect metadata
        seen = {}
        categories = {}
        source_paths = {}
        source_scores = {}
        image_heavy_sources = {}

        retrieved_nodes = (high_confidence_nodes if high_confidence_nodes else nodes)

        for node in retrieved_nodes:
            file_path = node.metadata.get("file_path", "")
            img_heavy = (node.metadata.get("image_heavy", "False") == "True")
            node_score = node.score or 0
            display_name = node.metadata.get("display_name", "Unknown source")
            drive_url = node.metadata.get("drive_url")
            category  = node.metadata.get("category", "")

            # Strip keys that shouldn't reach the LLM
            for key in EXCLUDED_LLM_KEYS:
                node.metadata.pop(key, None)

            # Deduplicate by display name, keeping highest score
            if display_name not in seen:
                seen[display_name] = drive_url
                categories[display_name] = category
                source_scores[display_name] = node_score

                if file_path:
                    source_paths[display_name] = file_path

                if img_heavy and file_path:
                    image_heavy_sources[display_name] = file_path
            else:
                if node_score > source_scores.get(display_name, 0):
                    source_scores[display_name] = node_score

        # Image only nodes
        if image_heavy_nodes and not text_nodes:
            return {
                "answer": (":frame_with_picture: The best available information for this question is in the images below."),
                "sources": seen,
                "categories": categories,
                "source_paths": source_paths,
                "source_scores": source_scores,
                "image_heavy_sources": image_heavy_sources
            }

        # Build context string
        context_parts = []
        for node in (text_nodes or high_confidence_nodes or nodes):
            text = node.get_content(metadata_mode=MetadataMode.NONE)
            if text.strip():
                context_parts.append(text.strip())

        context_str = "\n\n---\n\n".join(context_parts)

        # System prompt selection
        if high_confidence_nodes and text_nodes:
            prompt = (f"Context from internal knowledge base:\n{context_str}\n\nQuestion: {question}\n\nAnswer:")
            system = (SYSTEM_PROMPT + f"\n\n{feedback_hints}" if feedback_hints else SYSTEM_PROMPT)
        else:
            prompt = f"Question: {question}\n\nAnswer:"
            system = (FALLBACK_PROMPT + f"\n\n{feedback_hints}" if feedback_hints else FALLBACK_PROMPT)

        answer = gemini_query(prompt, system, history=history)

        return {
            "answer": answer,
            "sources": seen,
            "categories": categories,
            "source_paths": source_paths,
            "source_scores": source_scores,
            "image_heavy_sources": image_heavy_sources
        }

    except geminiUnavailable:
        return {**empty_result, "answer": (":warning: Gemini is currently busy or unavailable. Please try again in a few minutes. If the issue persists, please contact a senior support agent.")}

    except Exception as e:
        logger.error(f"Unexpected error in query(): {e}", exc_info=True)
        return {**empty_result, "answer": (":x: Something went wrong while processing your question, please try again. If the issue persists have a senior support agent check the server logs.")}
