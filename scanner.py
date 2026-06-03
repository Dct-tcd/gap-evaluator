name: Daily Stock Gap Scanner

on:
  schedule:
    # 8:45 AM IST converts to 3:15 AM UTC (Monday to Friday)
    - cron: '15 3 * * 1-5'
  workflow_dispatch:
    inputs:
      ticker:
        description: 'Optional: Enter a specific ticker to scan (e.g., WIPRO, RELIANCE)'
        required: false
        default: ''

jobs:
  run-scanner:
    runs-on: ubuntu-latest

    steps:
    - name: Checkout Repository
      uses: actions/checkout@v4

    - name: Set up Python
      uses: actions/setup-python@v5
      with:
        python-version: '3.10'

    - name: Install Dependencies
      run: |
        python -m pip install --upgrade pip
        pip install yfinance pandas numpy requests

    - name: Run Gap Predictor Script
      env:
        TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
        TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
      run: python scanner.py ${{ github.event.inputs.ticker }}
