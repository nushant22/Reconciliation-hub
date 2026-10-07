# Contributing to eSewa Reconciliation Engine

Thank you for considering contributing to the eSewa Reconciliation Engine!

## Getting Started

1. Fork the repository
2. Clone your fork: `git clone https://github.com/YOUR-USERNAME/Reconciliation.git`
3. Create a virtual environment: `python -m venv venv`
4. Activate it:
   - Windows: `venv\Scripts\activate`
   - macOS/Linux: `source venv/bin/activate`
5. Install dependencies: `pip install -r requirements.txt`
6. Run tests: `pytest`

## Development Workflow

1. Create a new branch: `git checkout -b feature/your-feature-name`
2. Make your changes
3. Run tests: `pytest`
4. Commit with clear messages: `git commit -m "Add feature: description"`
5. Push to your fork: `git push origin feature/your-feature-name`
6. Open a Pull Request

## Code Style

- Follow PEP 8 guidelines
- Add docstrings to new functions and classes
- Keep functions focused and single-purpose
- Add tests for new features

## Testing

- Write tests for new features in `backend/tests/`
- Ensure all tests pass before submitting PR
- Run full test suite: `pytest`
- Run specific test: `pytest backend/tests/test_matcher.py`

## Reporting Issues

- Use GitHub Issues
- Include clear reproduction steps
- Provide sample data if possible (anonymized)
- Specify your Python version and OS

## Questions?

Open a discussion or issue on GitHub.
