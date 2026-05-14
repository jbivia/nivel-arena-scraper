# NivelArena Card Scraper

A robust, containerized Python-based web scraper designed to extract high-resolution trading card images from [nivelarena.co.kr](http://nivelarena.co.kr). This tool automates the collection process, tracks progress via a local database to avoid redundant downloads, and includes post-processing capabilities to convert images into transparent PNGs.

## Features

- **Automated Scraping**: Iterates through board pagination to collect card metadata and high-resolution images.
- **Duplicate Prevention**: Uses a local SQLite database to track `wr_id` and prevent re-downloading existing content.
- **Respectful Crawling**: Implements randomized delays and standard headers to minimize server impact.
- **Containerized Execution**: Fully orchestrated with Podman/Docker Compose for a consistent environment.
- **Image Post-Processing**: Includes an OpenCV-powered script to convert JPG cards into transparent PNGs by removing white backgrounds with "leak" protection logic.
- **Security-Minded**: Runs as a non-root user within the container.

## Project Structure

```text
├── main.py              # Core scraper logic (Requests + BeautifulSoup)
├── convert_to_png.py    # Image processing script (OpenCV)
├── compose.yaml         # Container orchestration
├── Containerfile        # Container image definition
├── Makefile             # Command shortcuts for common tasks
├── requirements.txt     # Python dependencies
├── data/                # SQLite database storage (host-mounted)
├── downloads/           # Raw JPG downloads (host-mounted)
└── processed/           # Final transparent PNGs (host-mounted)
```

## Prerequisites

- **Podman** or **Docker**
- **Podman Compose** or **Docker Compose**
- **Make** (optional, but highly recommended)

## Quick Start

### 1. Setup Environment
Initialize the necessary directories and set correct permissions:

```bash
make setup
```

### 2. Start Scraping
Build and run the scraper in the foreground:

```bash
make up
```

To run in the background:

```bash
make up-d
```

### 3. View Progress
Follow the logs to monitor the scraping process:

```bash
make logs
```

### 4. Post-Process (Convert to PNG)
Once images are downloaded, you can run the transparency conversion script:

```bash
make convert
```

## Advanced Usage

| Command | Description |
| :--- | :--- |
| `make build` | Manually build the container image. |
| `make down` | Stop and remove the containers. |
| `make purge-db` | Reset scraping history (Deletes `scraper.db`). |
| `make purge-downloads` | Delete all downloaded images. |
| `make shell` | Open a shell inside the running container. |

## Configuration

Settings such as the `BASE_URL` and `BOARD_ID` can be modified in the `if __name__ == "__main__":` block of `main.py`.

```python
if __name__ == "__main__":
    BASE_URL = "http://nivelarena.co.kr"
    BOARD_ID = "cardlists"
```

## Security & Persistence

- **Data Persistence**: The container mounts `./data`, `./downloads`, and `./processed` to the host. This ensures your scraping history and images survive container restarts.
- **SELinux Support**: Volume mounts include the `:Z` label and `userns_mode: keep-id` for compatibility with rootless Podman on systems like Fedora or RHEL.

## Legal & Copyright Disclaimer

This project is a technical tool developed for educational and personal use only.

- **Code:** The scraper and processing scripts are open-source (see License).
- **Assets & Intellectual Property:** All trading card images, character designs, logos, text, and artwork downloaded or processed by this tool remain the exclusive intellectual property of their respective creators, publishers, and copyright holders. 

This includes, but is strictly not limited to:
- **Nivel / Nivel Arena** (TCG Publisher)
- **Shift Up** (*Goddess of Victory: NIKKE*)
- **Neowiz** (*Brown Dust 2*)
- **Smilegate** (*Epic Seven*)
- **Nimble Neuron** (*Eternal Return*)
- *Any other studio, publisher, or intellectual property featured in current or future Nivel Arena collaborations.*

This project is an independent, community-driven tool and is **not** affiliated with, endorsed by, sponsored by, or associated with any of these companies. Users of this tool are solely responsible for ensuring their usage complies with the target website's Terms of Service and local copyright or fair use laws.

## License

This project is licensed under the MIT License. You are free to use, modify, and distribute this software, provided that the original copyright notice and this permission notice are included in all copies or substantial portions of the software.

See the [LICENSE](LICENSE) file for more details.
