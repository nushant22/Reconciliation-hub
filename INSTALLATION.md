# Installation Guide

## Quick Start

### Prerequisites

- Python 3.9 or higher
- pip (Python package manager)
- Git

### Local Development Setup

1. **Clone the repository**
   ```bash
   git clone https://github.com/YOUR-USERNAME/Reconciliation.git
   cd Reconciliation
   ```

2. **Create a virtual environment**
   ```bash
   # Windows
   python -m venv venv
   venv\Scripts\activate

   # macOS/Linux
   python3 -m venv venv
   source venv/bin/activate
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Set up secrets (optional for Airlines page)**
   ```bash
   # Copy the example secrets file
   cp .streamlit/secrets.toml.example .streamlit/secrets.toml
   
   # Edit .streamlit/secrets.toml with your database credentials
   # Note: This file is gitignored and should never be committed
   ```

5. **Run the application**
   ```bash
   # Using streamlit command (if installed globally)
   streamlit run app.py

   # Or using Python module
   python -m streamlit run app.py
   ```

6. **Access the app**
   - Open your browser to http://localhost:8502

## Running Tests

```bash
# Run all tests
pytest

# Run with verbose output
pytest -v

# Run specific test file
pytest backend/tests/test_matcher.py

# Run with coverage
pytest --cov=backend
```

## Deployment Options

### Streamlit Community Cloud

1. Push your code to GitHub
2. Visit [share.streamlit.io](https://share.streamlit.io)
3. Connect your GitHub repository
4. Add secrets in App Settings → Secrets (copy from secrets.toml.example)
5. Deploy

### Docker (Production)

```bash
# Build image
docker build -t recon-engine .

# Run container
docker run -p 8080:8080 -v $(pwd)/airlines_state:/app/airlines_state recon-engine
```

### AWS/GCP/Azure

For production workloads with large files (>500k rows):

**Recommended specs:**
- 2-4 vCPUs
- 4-8 GB RAM
- 20 GB storage

**Setup example (Ubuntu/Debian):**
```bash
# Install Python
sudo apt update
sudo apt install python3.11 python3.11-venv python3-pip

# Clone and setup
git clone https://github.com/YOUR-USERNAME/Reconciliation.git
cd Reconciliation
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Run with specific port
streamlit run app.py --server.port 8080 --server.address 0.0.0.0
```

## Environment Variables

Set these in your deployment environment:

- `RECON_SHEET_ROW_CAP` — Max rows per output sheet (default: unlimited)
- `RECON_LOG_LEVEL` — Logging level (DEBUG, INFO, WARNING, ERROR)
- `RECON_AUDIT_DB` — Path to SQLite audit database
- `AIRLINES_STATE_DIR` — Directory for Airlines reconciliation state files

Example:
```bash
export RECON_SHEET_ROW_CAP=50000
export RECON_LOG_LEVEL=INFO
streamlit run app.py
```

## Troubleshooting

### Module not found errors
```bash
# Make sure you're in the virtual environment
# Windows: venv\Scripts\activate
# macOS/Linux: source venv/bin/activate

# Reinstall dependencies
pip install -r requirements.txt
```

### Port already in use
```bash
# Use a different port
streamlit run app.py --server.port 8503
```

### Memory issues with large files
- Use CSV output format instead of Excel
- Increase system RAM or split data into smaller chunks
- Set `RECON_SHEET_ROW_CAP=50000`

### Airlines state directory errors
```bash
# Create the directory manually
mkdir airlines_state

# Or set custom location
export AIRLINES_STATE_DIR=/path/to/state
```

## Updating

```bash
# Pull latest changes
git pull origin main

# Update dependencies
pip install -r requirements.txt --upgrade

# Run tests
pytest
```

## Support

- GitHub Issues: https://github.com/YOUR-USERNAME/Reconciliation/issues
- Documentation: See README.md and DASHBOARD.md
