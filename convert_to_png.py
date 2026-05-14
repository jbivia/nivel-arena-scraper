import cv2
import numpy as np
import logging
from pathlib import Path

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def process_image(file_path, output_dir, tolerance=10):
    """
    Core image processing logic. Returns (success, transparency_ratio).
    """
    img = cv2.imread(str(file_path))
    if img is None:
        logging.error(f"Could not read image: {file_path}")
        return False, 0.0

    # Convert to BGRA (add alpha channel)
    img_bgra = cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)
    h, w = img.shape[:2]
    total_pixels = h * w
    
    # Create a mask for floodFill (must be h+2, w+2)
    mask = np.zeros((h + 2, w + 2), np.uint8)
    
    # Define the 4 corners to start flood fill from
    corners = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]
    
    # flags: 4-connectivity, mask only, fill mask with value 1
    flags = 4 | cv2.FLOODFILL_MASK_ONLY | (1 << 8)
    
    filled_any = False
    for x, y in corners:
        # Seed Check: Only start if corner is "white-ish" (all BGR > 230)
        # Note: OpenCV uses BGR order
        pixel = img[y, x]
        if np.all(pixel > 230):
            if mask[y+1, x+1] == 0:
                cv2.floodFill(
                    img, mask, (x, y), 0,
                    loDiff=(tolerance, tolerance, tolerance),
                    upDiff=(tolerance, tolerance, tolerance),
                    flags=flags
                )
                filled_any = True

    # Extract the mask
    actual_mask = mask[1:-1, 1:-1]
    transparent_pixels = np.sum(actual_mask == 1)
    transparency_ratio = transparent_pixels / total_pixels
    
    # Apply transparency to the alpha channel
    img_bgra[actual_mask == 1, 3] = 0
    
    # Save image
    png_path = output_dir / (file_path.stem + ".png")
    if cv2.imwrite(str(png_path), img_bgra):
        return True, transparency_ratio
    
    return False, 0.0

def convert_with_safety(file_path, output_dir):
    """
    Attempts conversion with tolerance 10, retries with 3 if a leak is detected.
    """
    # Attempt 1: Standard tolerance
    success, ratio = process_image(file_path, output_dir, tolerance=10)
    
    if success:
        # Leak Sanity Check: If > 4.5% transparent, it's probably a leak
        if ratio > 0.045:
            logging.warning(f"Possible leak in {file_path.name} ({ratio:.1%} transparent). Retrying with tolerance 3...")
            success, ratio = process_image(file_path, output_dir, tolerance=3)
            
            if success and ratio > 0.045:
                logging.error(f"Persistent leak in {file_path.name} ({ratio:.1%} transparent). Skipping to protect artwork.")
                # Delete the "leaked" PNG if it was saved
                png_path = output_dir / (file_path.stem + ".png")
                if png_path.exists():
                    png_path.unlink()
                return False
        
        logging.info(f"Processed: {file_path.name} (Transparency: {ratio:.1%})")
        return True
    
    return False

def main():
    downloads_dir = Path("/app/downloads")
    processed_dir = Path("/app/processed")
    processed_dir.mkdir(parents=True, exist_ok=True)

    jpg_files = list(downloads_dir.glob("*.jpg"))
    if not jpg_files:
        logging.warning("No .jpg files found.")
        return

    logging.info(f"Starting safety-optimized conversion for {len(jpg_files)} images...")

    success_count = 0
    for jpg_file in jpg_files:
        if convert_with_safety(jpg_file, processed_dir):
            success_count += 1

    logging.info(f"Finished. Successfully processed {success_count}/{len(jpg_files)} images.")

if __name__ == "__main__":
    main()
