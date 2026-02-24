@echo off
chcp 65001 >nul
cd /d "d:\project\aineoo.com\code"
python main.py toutiao republish "output\ai-agent-enterprise-implementation-guide-2026\toutiao_content.json" -y
