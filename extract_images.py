import os
import logging
import fitz

logger = logging.getLogger(__name__)

# Byte signatures for image formats
IMG_BYTES = [
    b'\x89PNG',       # PNG
    b'\xff\xd8\xff',  # JPEG
    b'RIFF',          # WEBP
    b'GIF8',          # GIF
]


# Return True if bytes appear to be a supported image format
def image_format(data: bytes) -> bool:
    if len(data) < 4:
        return False
    for byte in IMG_BYTES:
        if data[:len(byte)] == byte:
            return True
    return False


# Extract embedded images from pdfs
def get_embedded_images(filepath: str, output_dir: str) -> list:
    try:
        doc = fitz.open(filepath)
    except Exception as e:
        logger.error(f"Failed to open {os.path.basename(filepath)}: {e}", exc_info=True)
        return []

    image_paths = []
    filename = os.path.splitext(os.path.basename(filepath))[0]
    os.makedirs(output_dir, exist_ok=True)

    for page_num, page in enumerate(doc):
        try:
            image_list = page.get_images(full=True)
        except Exception as e:
            logger.warning(f"Failed to get images from page {page_num + 1} of {os.path.basename(filepath)}: {e}")
            continue

        for img_index, img in enumerate(image_list):
            index_ref = img[0]
            try:
                base_image = doc.extract_image(index_ref)
                image_bytes = base_image["image"]
                image_ext = base_image["ext"]
                width = base_image.get("width", 0)
                height = base_image.get("height", 0)

                # Skip tiny decorative images
                if width < 150 or height < 150:
                    continue

                # Validate bytes before saving
                if not image_format(image_bytes):
                    logger.warning(f"Skipping invalid image bytes: page {page_num + 1}, img {img_index + 1} in {os.path.basename(filepath)}")
                    continue

                image_path = os.path.join(output_dir,f"{filename}_p{page_num + 1}_i{img_index + 1}.{image_ext}")

                with open(image_path, "wb") as f:
                    f.write(image_bytes)

                if os.path.exists(image_path):
                    image_paths.append((image_path, page_num + 1))

            except Exception as e:
                logger.warning(f"Failed to extract image {img_index + 1} from page {page_num + 1} of {os.path.basename(filepath)}: {e}")

    doc.close()
    logger.info(f"Extracted {len(image_paths)} embedded images from {os.path.basename(filepath)}")
    return image_paths


# Fallback function: render full pages as images, prioritising pages whose text contains any of the provided keywords. Returns list of (image_path, page_num) tuples.
def get_pdf_pages(filepath: str, output_dir: str, keywords: list, max_pages: int = 3) -> list:
    try:
        doc = fitz.open(filepath)
    except Exception as e:
        logger.error(f"Failed to open {os.path.basename(filepath)}: {e}", exc_info=True)
        return []

    filename = os.path.splitext(os.path.basename(filepath))[0]
    os.makedirs(output_dir, exist_ok=True)

    matched_pages = []
    for page_num, page in enumerate(doc):
        page_text = page.get_text().lower()
        if any(kw.lower() in page_text for kw in keywords):
            matched_pages.append(page_num)

    # No keyword matches — fall back to first max_pages pages
    if not matched_pages:
        matched_pages = list(range(min(max_pages, len(doc))))

    image_paths = []
    for page_num in matched_pages[:max_pages]:
        page = doc[page_num]
        try:
            mat = fitz.Matrix(150 / 72, 150 / 72)
            pix = page.get_pixmap(matrix=mat)
            image_path = os.path.join(output_dir, f"{filename}_page{page_num + 1}.png")
            pix.save(image_path)
            if os.path.exists(image_path):
                image_paths.append((image_path, page_num + 1))
        except Exception as e:
            logger.warning(f"Failed to render page {page_num + 1} of {os.path.basename(filepath)}: {e}")

    doc.close()
    logger.info(f"Rendered {len(image_paths)} pages from {os.path.basename(filepath)}")
    return image_paths


# Primary entry point, tries embedded image extraction first. Scores images by keyword relevance if keywords are provided. 
# Falls back to keyword-matched page rendering if no embedded images are found. Returns list of (image_path, page_num) tuples, capped at max_images.
def pdf_images(filepath: str, output_dir: str, keywords: list = None, max_images: int = 5) -> list:
    images = get_embedded_images(filepath, output_dir)

    if images:
        if keywords:
            try:
                doc = fitz.open(filepath)
                scored = []
                for image_path, page_num in images:
                    page = doc[page_num - 1]
                    page_text = page.get_text().lower()
                    score = sum(1 for kw in keywords if kw.lower() in page_text)
                    scored.append((image_path, page_num, score))
                doc.close()

                # Sort by relevance, then page order 
                scored.sort(key=lambda x: (-x[2], x[1]))
                images = [(p, n) for p, n, _ in scored]
            except Exception as e:
                logger.warning(f"Keyword scoring failed: {e}")

        return images[:max_images]

    logger.info(f"No embedded images found in {os.path.basename(filepath)}, falling back to page render")

    return get_pdf_pages(filepath, output_dir, keywords or [], max_pages=min(max_images, 3))