# Contributing to TFT911 EAS Logger

## Getting Started

1. Fork the repository
2. Clone your fork: `git clone https://github.com/YOUR_USERNAME/tft911-eas.git`
3. Create a branch: `git checkout -b feature/your-feature`
4. Run the install script: `bash install.sh`

## Development

```bash
# Activate environment
source venv/bin/activate

# Make changes to code
# Test your changes
python3 virtual_tft.py 1 | python3 TFT_EAS_911_Pi_logger.py

# Run linter
pylint TFT_EAS_911_Pi_logger.py virtual_tft.py
```

## Submitting Changes

1. Ensure your code is clean and well-commented
2. Test on both Python 3.10+ versions
3. Commit with clear messages: `git commit -m "Add feature: description"`
4. Push to your fork: `git push origin feature/your-feature`
5. Create a Pull Request with description of changes

## Code Style

- Follow PEP 8
- Use type hints where practical
- Keep functions focused and well-documented
- Maximum line length: 120 characters

## Bug Reports

Please include:
- Python version (`python3 --version`)
- Operating system
- Steps to reproduce
- Expected vs actual behavior
- Error messages/stack traces
