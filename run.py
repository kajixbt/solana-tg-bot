#!/usr/bin/env python3
"""
Entry point — loads .env then starts the bot.
Run: python run.py  (or: python -m bot)
"""
from dotenv import load_dotenv
load_dotenv()

from bot import main
main()
