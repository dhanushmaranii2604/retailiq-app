import json
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()

API_KEY = os.getenv("GROQ_API_KEY")
MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
client = AsyncOpenAI(api_key=API_KEY, base_url="https://api.groq.com/openai/v1")

IST = ZoneInfo("Asia/Kolkata")
MAX_TURNS = 6
MAX_TOOL_ROUNDS = 5


class DailyLimit(Exception):
    pass


def system_prompt(language: str):
    now = datetime.now(IST)
    return (
        "You are RetailIQ: AI Sales & Stock Assistant. "
        "Use the available tools to answer questions about sales, stock, and udhaar (credits). "
        f"CRITICAL: You MUST reply ONLY in {language}. Even if the user types in English, you must translate your answer to {language}. "
        "If the user says 'Ramesh owes me 500', use the add_udhaar tool. "
        "If the user asks 'Who owes me money?', use get_pending_udhaar. "
        "If the user says 'Ramesh paid', use clear_udhaar.\n"
        "IMPORTANT FORMATTING RULE: If you are returning a list of udhaar, use a Markdown table. "
        "HOWEVER, if the tool returns a message saying there is no pending udhaar (or 'note'), do NOT create an empty table. Just reply with a simple, friendly sentence stating there are no pending balances."
    )


def clean_schema(node):
    if isinstance(node, list):
        return [clean_schema(x) for x in node]
    if not isinstance(node, dict):
        return node
    if "anyOf" in node:
        options = [o for o in node["anyOf"] if o.get("type") != "null"]
        if options:
            merged = {k: v for k, v in node.items() if k != "anyOf"}
            merged.update(options[0])
            return clean_schema(merged)
    return {k: clean_schema(v) for k, v in node.items()}


class RetailIQAgent:
    def __init__(self, session, gemini_tool):
        self.session = session
        self.history = {}
        
        self.tools = []
        for fn in gemini_tool.function_declarations:
            self.tools.append({
                "type": "function",
                "function": {
                    "name": fn.name,
                    "description": fn.description,
                    "parameters": clean_schema(fn.parameters_json_schema)
                }
            })

    async def ask(self, session_id: str, text: str, language: str = "English"):
        # Wipe history if language changes to prevent mixed-language responses
        if session_id not in self.history or self.history[session_id].get("lang") != language:
            self.history[session_id] = {
                "lang": language,
                "messages": [{"role": "system", "content": system_prompt(language)}]
            }
        
        messages = self.history[session_id]["messages"]
        messages.append({"role": "user", "content": text})
        
        if len(messages) > 20:
            messages = [messages[0]] + messages[-MAX_TURNS:]
            self.history[session_id]["messages"] = messages

        for _ in range(MAX_TOOL_ROUNDS):
            try:
                response = await client.chat.completions.create(
                    model=MODEL,
                    messages=messages,
                    tools=self.tools,
                    tool_choice="auto",
                    max_tokens=1024,
                    temperature=0.1
                )
            except Exception as e:
                logging.error(f"Groq API Error: {e}")
                return f"AI Error: {str(e)}"
            
            msg = response.choices[0].message
            msg_dict = msg.model_dump(exclude_unset=True)
            messages.append(msg_dict)
            
            if not msg.tool_calls:
                return msg.content

            for tool_call in msg.tool_calls:
                name = tool_call.function.name
                try:
                    args = json.loads(tool_call.function.arguments)
                except json.JSONDecodeError:
                    args = {}
                
                logging.info(f"Running tool: {name} with args: {args}")
                try:
                    result = await self.session.call_tool(name, args)
                    content = ""
                    for c in result.content or []:
                        if hasattr(c, 'text'):
                            content += c.text
                    
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": name,
                        "content": content or "No data returned."
                    })
                except Exception as e:
                    logging.error(f"Tool {name} failed: {e}")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": name,
                        "content": f"Error: {str(e)}"
                    })

        return "Sorry, I took too many steps to answer. Please try a simpler question."