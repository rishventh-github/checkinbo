import discord
from discord.ext import commands, tasks
from datetime import datetime, time as datetime_time, timedelta, time
import pytz
import os
import google.generativeai as genai
from dotenv import load_dotenv
import psycopg2
import psycopg2.extras
import re
import json
from dateutil import parser
from io import BytesIO
from PIL import Image
import asyncio


load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")


# --- Database Connection Parameters for Local PostgreSQL ---
# These are for local development. On Heroku, DATABASE_URL will be used.
DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT")
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")


DATABASE_URL = os.getenv("DATABASE_URL")  # Heroku automatically sets this


DATABASE_TABLE_NAME = "channel_settings_data"  # Renamed for clarity, holds core settings
LEADERBOARD_CHECKIN_TABLE = "checkin_leaderboard"
LEADERBOARD_MISSED_TABLE = "missed_leaderboard"
# ---------------------------------------------------------


bot = commands.Bot(command_prefix=commands.when_mentioned_or("c.", "C."), intents=discord.Intents.all())


# In-memory cache for guild and channel data
# Structure: {guild_id: {channel_id: data_dict, 0: guild_settings}}
guild_channel_data_cache = {}




def get_db_connection():
   """Establishes and returns a connection to the PostgreSQL database."""
   try:
       if DATABASE_URL:
           # Use DATABASE_URL for Heroku deployment
           conn = psycopg2.connect(DATABASE_URL)
           print("INFO: Connected to PostgreSQL using DATABASE_URL.")
       else:
           # Fallback to individual env vars for local development
           conn = psycopg2.connect(
               host=DB_HOST,
               port=DB_PORT,
               database=DB_NAME,
               user=DB_USER,
               password=DB_PASSWORD
           )
           print("INFO: Connected to PostgreSQL using local environment variables.")
       return conn
   except psycopg2.Error as e:
       print(f"ERROR: Error connecting to PostgreSQL: {e}")
       return None




def initialize_database():
   """Ensures the necessary tables exist in the PostgreSQL database."""
   conn = get_db_connection()
   if conn:
       cur = None
       try:
           cur = conn.cursor()


           # Create table for channel settings (formerly checkindata_per_channel)
           cur.execute(f"""
               CREATE TABLE IF NOT EXISTS {DATABASE_TABLE_NAME} (
                   guild_id BIGINT NOT NULL,
                   channel_id BIGINT NOT NULL,
                   data JSONB,
                   PRIMARY KEY (guild_id, channel_id)
               );
           """)
           print(f"INFO: {DATABASE_TABLE_NAME} table ensured.")


           # Create table for check-in leaderboard
           cur.execute(f"""
               CREATE TABLE IF NOT EXISTS {LEADERBOARD_CHECKIN_TABLE} (
                   guild_id BIGINT NOT NULL,
                   channel_id BIGINT NOT NULL,
                   user_id BIGINT NOT NULL,
                   user_name VARCHAR(255) NOT NULL,
                   count INTEGER DEFAULT 0,
                   PRIMARY KEY (guild_id, channel_id, user_id)
               );
           """)
           print(f"INFO: {LEADERBOARD_CHECKIN_TABLE} table ensured.")


           # Create table for missed check-in leaderboard
           cur.execute(f"""
               CREATE TABLE IF NOT EXISTS {LEADERBOARD_MISSED_TABLE} (
                   guild_id BIGINT NOT NULL,
                   channel_id BIGINT NOT NULL,
                   user_id BIGINT NOT NULL,
                   user_name VARCHAR(255) NOT NULL,
                   count INTEGER DEFAULT 0,
                   PRIMARY KEY (guild_id, channel_id, user_id)
               );
           """)
           print(f"INFO: {LEADERBOARD_MISSED_TABLE} table ensured.")


           conn.commit()
           print("INFO: All necessary database tables are ready.")


       except (Exception, psycopg2.Error) as error:
           print(f"ERROR: Error while initializing database: {error}")
           if conn:
               conn.rollback()
       finally:
           if cur:
               cur.close()
           if conn:
               conn.close()




def convert_sets_to_lists(obj):
   """Recursively converts sets to lists and datetime objects to ISO strings for JSON serialization."""
   if isinstance(obj, set):
       return list(obj)
   elif isinstance(obj, list):
       return [convert_sets_to_lists(elem) for elem in obj]
   elif isinstance(obj, dict):
       return {k: convert_sets_to_lists(v) for k, v in obj.items()}
   elif isinstance(obj, datetime):
       return obj.isoformat()  # Convert datetime to ISO 8601 string
   return obj




async def load_specific_data_from_db(guild_id, channel_id):
   """Loads data for a specific guild-channel pair from the database."""
   conn = get_db_connection()
   if conn:
       cur = None
       try:
           cur = conn.cursor()
           loaded_data = {}


           # Load core channel settings
           cur.execute(f"SELECT data FROM {DATABASE_TABLE_NAME} WHERE guild_id = %s AND channel_id = %s",
                       (guild_id, channel_id))
           record = cur.fetchone()
           if record and record[0]:
               loaded_data = record[0]
               if "banned_users" in loaded_data and isinstance(loaded_data["banned_users"], list):
                   loaded_data["banned_users"] = set(loaded_data["banned_users"])
               if "server_admins" in loaded_data and isinstance(loaded_data["server_admins"],
                                                                list):  # For guild settings (channel_id=0)
                   loaded_data["server_admins"] = set(loaded_data["server_admins"])
               if "last_reset_time" in loaded_data and isinstance(loaded_data["last_reset_time"], str):
                   try:
                       dt_obj = datetime.fromisoformat(loaded_data["last_reset_time"])
                       if dt_obj.tzinfo is None:
                           loaded_data["last_reset_time"] = dt_obj.replace(tzinfo=pytz.utc)
                       else:
                           loaded_data["last_reset_time"] = dt_obj
                   except ValueError:
                       loaded_data["last_reset_time"] = None
           else:
               loaded_data = {}  # Ensure it's a dict even if no record found


           # Load check-in leaderboard
           checkin_users = {}
           cur.execute(
               f"SELECT user_id, count FROM {LEADERBOARD_CHECKIN_TABLE} WHERE guild_id = %s AND channel_id = %s",
               (guild_id, channel_id))
           for user_id, count in cur.fetchall():
               checkin_users[user_id] = count
           loaded_data["users"] = checkin_users


           # Load missed check-in leaderboard
           missed_users = {}
           cur.execute(
               f"SELECT user_id, count FROM {LEADERBOARD_MISSED_TABLE} WHERE guild_id = %s AND channel_id = %s",
               (guild_id, channel_id))
           for user_id, count in cur.fetchall():
               missed_users[user_id] = count
           loaded_data["missed_users"] = missed_users


           return loaded_data
       except (Exception, psycopg2.Error) as error:
           print(
               f"ERROR: Error while fetching data for guild {guild_id}, channel {channel_id} from PostgreSQL: {error}")
           return None
       finally:
           if cur:
               cur.close()
           if conn:
               conn.close()
   else:
       print("ERROR: Could not establish database connection for loading.")
       return None




async def save_specific_data_to_db(guild_id, channel_id, data):
   """
   Saves or updates data for a specific guild-channel pair in the database.
   This now handles separate tables for core settings and leaderboards.
   """
   conn = get_db_connection()
   if conn:
       cur = None
       try:
           cur = conn.cursor()


           # Separate core channel data from leaderboard data
           core_data_to_save = data.copy()
           checkin_leaderboard_data = core_data_to_save.pop("users", {})
           missed_leaderboard_data = core_data_to_save.pop("missed_users", {})
           user_to_real_mapping = core_data_to_save.get("userToReal",
                                                        {})  # Used for saving user_name in leaderboard tables


           # Convert sets/datetimes in core_data before JSONB serialization
           core_data_for_json = convert_sets_to_lists(core_data_to_save)


           # --- Save core channel settings (JSONB table) ---
           cur.execute(
               f"""
               INSERT INTO {DATABASE_TABLE_NAME} (guild_id, channel_id, data)
               VALUES (%s, %s, %s)
               ON CONFLICT (guild_id, channel_id) DO UPDATE
               SET data = %s
               """,
               (guild_id, channel_id, psycopg2.extras.Json(core_data_for_json),
                psycopg2.extras.Json(core_data_for_json))
           )
           print(f"DEBUG: Saved core settings for Guild {guild_id}, Channel {channel_id}: {core_data_for_json}")


           # --- Save check-in leaderboard ---
           # Delete existing entries for this channel to ensure consistency
           cur.execute(f"DELETE FROM {LEADERBOARD_CHECKIN_TABLE} WHERE guild_id = %s AND channel_id = %s",
                       (guild_id, channel_id))
           print(f"DEBUG: Deleted old check-in leaderboard entries for Guild {guild_id}, Channel {channel_id}.")


           # Insert current check-in leaderboard data
           if checkin_leaderboard_data:
               checkin_values = []
               for user_id, count in checkin_leaderboard_data.items():
                   # Get the user_name from the userToReal mapping, fallback to generic
                   user_name = user_to_real_mapping.get(str(user_id), f"User_{user_id}")
                   checkin_values.append((guild_id, channel_id, user_id, user_name, count))


               # Using execute_values for efficient bulk insert
               psycopg2.extras.execute_values(
                   cur,
                   f"INSERT INTO {LEADERBOARD_CHECKIN_TABLE} (guild_id, channel_id, user_id, user_name, count) VALUES %s",
                   checkin_values
               )
               print(
                   f"DEBUG: Saved check-in leaderboard for Guild {guild_id}, Channel {channel_id}: {checkin_leaderboard_data}")
           else:
               print(f"DEBUG: No check-in leaderboard data to save for Guild {guild_id}, Channel {channel_id}.")


           # --- Save missed check-in leaderboard ---
           # Delete existing entries for this channel to ensure consistency
           cur.execute(f"DELETE FROM {LEADERBOARD_MISSED_TABLE} WHERE guild_id = %s AND channel_id = %s",
                       (guild_id, channel_id))
           print(f"DEBUG: Deleted old missed check-in leaderboard entries for Guild {guild_id}, Channel {channel_id}.")


           # Insert current missed check-in leaderboard data
           if missed_leaderboard_data:
               missed_values = []
               for user_id, count in missed_leaderboard_data.items():
                   user_name = user_to_real_mapping.get(str(user_id), f"User_{user_id}")
                   missed_values.append((guild_id, channel_id, user_id, user_name, count))


               psycopg2.extras.execute_values(
                   cur,
                   f"INSERT INTO {LEADERBOARD_MISSED_TABLE} (guild_id, channel_id, user_id, user_name, count) VALUES %s",
                   missed_values
               )
               print(
                   f"DEBUG: Saved missed check-in leaderboard for Guild {guild_id}, Channel {channel_id}: {missed_leaderboard_data}")
           else:
               print(f"DEBUG: No missed check-in leaderboard data to save for Guild {guild_id}, Channel {channel_id}.")


           conn.commit()
           print(f"INFO: All data saved successfully for guild {guild_id}, channel {channel_id}.")
       except (Exception, psycopg2.Error) as error:
           print(f"ERROR: Error while saving data for guild {guild_id}, channel {channel_id} to PostgreSQL: {error}")
           if conn:
               conn.rollback()
       finally:
           if cur:
               cur.close()
           if conn:
               conn.close()
   else:
       print("ERROR: Could not establish database connection for saving.")




async def load_all_data_from_db():
   """Loads all existing guild and channel data from the database into the cache."""
   global guild_channel_data_cache
   guild_channel_data_cache = {}  # Clear cache before loading


   conn = get_db_connection()
   if conn:
       cur = None
       try:
           cur = conn.cursor()
           # Fetch all core channel/guild settings
           cur.execute(f"SELECT guild_id, channel_id, data FROM {DATABASE_TABLE_NAME}")
           records = cur.fetchall()
           for row in records:
               guild_id, channel_id, loaded_data = row
               if guild_id not in guild_channel_data_cache:
                   guild_channel_data_cache[guild_id] = {}


               if loaded_data:
                   if "banned_users" in loaded_data and isinstance(loaded_data["banned_users"], list):
                       loaded_data["banned_users"] = set(loaded_data["banned_users"])
                   if "server_admins" in loaded_data and isinstance(loaded_data["server_admins"], list):
                       loaded_data["server_admins"] = set(loaded_data["server_admins"])
                   if "last_reset_time" in loaded_data and isinstance(loaded_data["last_reset_time"], str):
                       try:
                           dt_obj = datetime.fromisoformat(loaded_data["last_reset_time"])
                           if dt_obj.tzinfo is None:
                               loaded_data["last_reset_time"] = dt_obj.replace(tzinfo=pytz.utc)
                           else:
                               loaded_data["last_reset_time"] = dt_obj
                       except ValueError:
                           loaded_data["last_reset_time"] = None
               guild_channel_data_cache[guild_id][channel_id] = loaded_data or {}  # Ensure it's always a dict


           # Fetch and populate check-in leaderboards
           cur.execute(f"SELECT guild_id, channel_id, user_id, count FROM {LEADERBOARD_CHECKIN_TABLE}")
           for guild_id, channel_id, user_id, count in cur.fetchall():
               if guild_id not in guild_channel_data_cache:
                   guild_channel_data_cache[guild_id] = {}
               if channel_id not in guild_channel_data_cache[guild_id]:
                   guild_channel_data_cache[guild_id][channel_id] = {}
               if "users" not in guild_channel_data_cache[guild_id][channel_id]:
                   guild_channel_data_cache[guild_id][channel_id]["users"] = {}
               guild_channel_data_cache[guild_id][channel_id]["users"][user_id] = count


           # Fetch and populate missed leaderboards
           cur.execute(f"SELECT guild_id, channel_id, user_id, count FROM {LEADERBOARD_MISSED_TABLE}")
           for guild_id, channel_id, user_id, count in cur.fetchall():
               if guild_id not in guild_channel_data_cache:
                   guild_channel_data_cache[guild_id] = {}
               if channel_id not in guild_channel_data_cache[guild_id]:
                   guild_channel_data_cache[guild_id][channel_id] = {}
               if "missed_users" not in guild_channel_data_cache[guild_id][channel_id]:
                   guild_channel_data_cache[guild_id][channel_id]["missed_users"] = {}
               guild_channel_data_cache[guild_id][channel_id]["missed_users"][user_id] = count


           print("INFO: All guild and channel data loaded from PostgreSQL.")
       except (Exception, psycopg2.Error) as error:
           print(f"ERROR: Error while loading all data from PostgreSQL: {error}")
       finally:
           if cur:
               cur.close()
           if conn:
               conn.close()




async def get_guild_settings(guild_id):
   """
   Retrieves guild-specific settings from the cache, loading from DB if not present.
   Creates default settings if neither cache nor DB has them.
   Guild settings are stored with channel_id = 0.
   """
   if guild_id not in guild_channel_data_cache:
       guild_channel_data_cache[guild_id] = {}


   if 0 not in guild_channel_data_cache[guild_id]:
       loaded_settings = await load_specific_data_from_db(guild_id, 0)


       # Initialize default settings
       default_settings = {
           "server_admins": set(),
           "timezone": "America/Los_Angeles",
           "newReset": "235959",
           "last_reset_time": None
       }


       if loaded_settings:
           # Update default settings with loaded settings,
           # ensuring all default keys are present.
           # Convert any list-like server_admins to set.
           if "server_admins" in loaded_settings and isinstance(loaded_settings["server_admins"], list):
               loaded_settings["server_admins"] = set(loaded_settings["server_admins"])


           default_settings.update(loaded_settings)
           guild_channel_data_cache[guild_id][0] = default_settings
           print(f"INFO: Loaded and merged guild settings for guild {guild_id}.")
       else:
           guild_channel_data_cache[guild_id][0] = default_settings
           await save_specific_data_to_db(guild_id, 0, guild_channel_data_cache[guild_id][0])
           print(f"INFO: Initialized and saved default guild settings for guild {guild_id}.")


   return guild_channel_data_cache[guild_id][0]




async def get_channel_data(guild_id, channel_id):
   """
   Retrieves channel-specific data from the cache, loading from DB if not present.
   Creates default data if neither cache nor DB has it.
   """
   if guild_id not in guild_channel_data_cache:
       guild_channel_data_cache[guild_id] = {}


   if channel_id not in guild_channel_data_cache[guild_id]:
       loaded_data = await load_specific_data_from_db(guild_id, channel_id)


       # Define default data for a new channel
       default_channel_data = {
           "users": {},  # Check-in counts {user_id: count}
           "dailyCheckedUsers": [],  # Users who checked in today
           "userToReal": {},  # Mapping of Discord ID to real name (string ID to string real name)
           "realPeople": {},  # Stores real names keyed by Discord ID (string ID to string real name)
           "banned_users": set(),  # Set of user_ids banned from checking in
           "require_media": False,
           "word_min": 1,
           "missed_users": {},  # Count of missed check-ins {user_id: count}
           "reset_time": None,  # Channel-specific reset time (HHMMSS string)
           "last_reset_time": None  # Last time this channel was reset (datetime object)
       }


       if loaded_data:
           # Merge loaded_data into default_channel_data to ensure all default keys are present
           if "banned_users" in loaded_data and isinstance(loaded_data["banned_users"], list):
               loaded_data["banned_users"] = set(loaded_data["banned_users"])


           default_channel_data.update(
               loaded_data)  # This will add any missing keys from default_channel_data if they were not in loaded_data
           guild_channel_data_cache[guild_id][channel_id] = default_channel_data
           print(f"INFO: Loaded and merged channel data for guild {guild_id}, channel {channel_id}.")
       else:
           guild_channel_data_cache[guild_id][channel_id] = default_channel_data
           await save_specific_data_to_db(guild_id, channel_id,
                                          guild_channel_data_cache[guild_id][channel_id])  # Save new defaults
           print(f"INFO: Initialized and saved default channel data for guild {guild_id}, channel {channel_id}.")
   return guild_channel_data_cache[guild_id][channel_id]




# Helper function to check if the user is an admin
async def is_admin(ctx):
   guild_settings = await get_guild_settings(ctx.guild.id)
   # Check if the author is the guild owner or in the server_admins set
   return ctx.author.id == ctx.guild.owner_id or ctx.author.id in guild_settings.get("server_admins", set())




@bot.command()
async def m(ctx):
   """Displays the manual for all bot commands."""
   await ctx.send("**Commands accessible by everyone**:"
                  "\n`c.m` - Manual"
                  "\n`c.c` - Post check-in (channel-specific)"
                  "\n`c.t` - Check who sent a check-in today (channel-specific)"
                  "\n`c.wl` - Leaderboard/Streak for checking in (channel-specific)"
                  "\n`c.ll` - Leaderboard/Streak for NOT checking in (channel-specific)"
                  "\n`c.cr` - Shows the current reset time for this channel and timezone for this guild"
                  "\n\n**Commands only accessible by server admins**:"
                  "\n`c.n` - Tracks certain users/changes usernames to their real names (channel-specific)"
                  "\n`c.a` - Adds/removes a certain number of check-ins to a user's check-in count (negative number to remove check-ins) (channel-specific)"
                  "\n`c.r` - Sets the reset time for check-ins for this channel"
                  "\n`c.e` - Requires evidence for check-ins (channel-specific)"
                  "\n`c.d` - Manages banned users (ban, unban, list) (channel-specific)"
                  "\n`c.g` - Manages server admins (guild-wide)"
                  "\n`c.tz` - Lists all timezones available (guild-wide)"
                  "\n`c.w` - Sets a minimum number of words required in the check-in (channel-specific)"
                  "\n`c.lr` - Reset the leaderboard, type in 'wl' or 'll' to choose which leaderboard to reset (channel-specific)"
                  "\n`c.sum` - Gives a summary of the check-ins for the day (you can specify date with MM-DD format), week, or month, and if the evidence was relevant or not")




async def set_admin_for_guild(guild):
   """
   Sets the server admin for a guild. Prioritizes the inviter if available
   from audit logs, otherwise defaults to the guild owner. This is a guild-wide setting.
   """
   guild_settings = await get_guild_settings(guild.id)


   # If the server already has admins, do nothing
   if guild_settings.get("server_admins"):  # Use .get() in case it's not initialized yet
       print(f"INFO: Guild {guild.name} ({guild.id}) already has admins set. Skipping initial admin setup.")
       return


   inviter = None
   try:
       # Fetch audit logs to find who added the bot
       async for entry in guild.audit_logs(action=discord.AuditLogAction.bot_add, limit=1):
           if entry.target.id == bot.user.id:
               inviter = entry.user
               break
   except discord.Forbidden:
       print(
           f"WARNING: Missing permissions to access audit logs in {guild.name} ({guild.id}). Cannot determine inviter.")


   if inviter:
       guild_settings["server_admins"].add(inviter.id)
       print(f"INFO: Admin for {guild.name} ({guild.id}) set to inviter: {inviter.name} ({inviter.id})")
   else:
       # Fallback to guild owner if inviter cannot be determined
       guild_settings["server_admins"].add(guild.owner_id)
       print(f"INFO: Admin for {guild.name} ({guild.id}) set to owner: {guild.owner.name} ({guild.owner_id})")


   await save_specific_data_to_db(guild.id, 0, guild_settings)  # Save guild-wide settings
   print(f"INFO: Current admins for {guild.name}: {guild_settings['server_admins']}")




@bot.event
async def on_guild_join(guild):
   """Event handler for when the bot joins a new server."""
   print(f"INFO: Joined new guild: {guild.name} ({guild.id})")
   await set_admin_for_guild(guild)




@bot.command()
async def g(ctx, action: str = None, member: discord.Member = None): # Make action optional
   """
   Manages server administrators: add, remove, or list.
   Only existing admins (or guild owner) can use this command. This is a guild-wide setting.
   """
   guild_id = ctx.guild.id
   guild_settings = await get_guild_settings(guild_id)


   if not await is_admin(ctx):  # Check if the author is an admin or owner
       await ctx.send(f"{ctx.author.mention}, this command is only accessible to server admins.")
       return


   if action is None: # New check for no arguments
       await ctx.send("Please specify an action. Usage: "
                      "`c.g add @User` to add a server admin, "
                      "`c.g remove @User` to remove a server admin, or "
                      "`c.g list` to view current server admins.")
       return


   action = action.lower()


   if action == "add" and member:
       if member.id in guild_settings["server_admins"]:
           await ctx.send(f"{member.mention} is already a server admin.")
       else:
           guild_settings["server_admins"].add(member.id)
           await ctx.send(f"{member.mention} has been added as a server admin.")
           await save_specific_data_to_db(guild_id, 0, guild_settings)  # Save changes
           print(f"INFO: {member.display_name} ({member.id}) added as admin for guild {guild_id}.")
   elif action == "remove" and member:
       if member.id == ctx.guild.owner_id:
           await ctx.send(f"{member.mention}, the guild owner cannot be removed from admin status.")
       elif member.id in guild_settings["server_admins"]:
           guild_settings["server_admins"].remove(member.id)
           await ctx.send(f"{member.mention} has been removed as a server admin.")
           await save_specific_data_to_db(guild_id, 0, guild_settings)  # Save changes
           print(f"INFO: {member.display_name} ({member.id}) removed from admins for guild {guild_id}.")
       else:
           await ctx.send(f"{member.mention} is not currently a server admin.")
   elif action == "list":
       admin_list = []
       for admin_id in guild_settings["server_admins"]:
           try:
               admin_user = await bot.fetch_user(admin_id)
               admin_list.append(f"{admin_user.display_name} (<@{admin_id}>)")
           except (discord.NotFound, discord.HTTPException):
               admin_list.append(f"Unknown User (<@{admin_id}>)")
       admins = "\n".join(admin_list) if admin_list else "No admins assigned."
       await ctx.send(f"**Server Admins:**\n{admins}")
   else: # This 'else' block will now only be hit if action is provided but is invalid (e.g., c.g invalid_action)
       await ctx.send("Invalid command. Usage: "
                      "`c.g add @User`, `c.g remove @User`, or `c.g list`.")




@bot.command()
async def c(ctx, *checkIn):
   """Check-in command for users. Handles check-ins and saves to Postgres immediately."""
   data = await get_channel_data(ctx.guild.id, ctx.channel.id)
   guild_id = ctx.guild.id
   channel_id = ctx.channel.id
   user_id = ctx.author.id


   # If banned
   if user_id in data.get("banned_users", set()):
       await ctx.send(f"{ctx.author.mention}, you are banned from checking in.")
       return


   # Ensure 'dailyCheckedUsers' is a list and 'users' is a dict, defensively
   if not isinstance(data.get("dailyCheckedUsers"), list):
       data["dailyCheckedUsers"] = []
   if not isinstance(data.get("users"), dict):
       data["users"] = {}


   # If already checked in today
   if user_id in data["dailyCheckedUsers"]: # Now safe to access directly
       await ctx.send(f"{ctx.author.mention}, you've already checked in today in this channel!")
       return


   # If require_media is set, check for attachments or links
   if data.get("require_media", False):
       has_attachment = bool(ctx.message.attachments)
       # Check for presence of common URL schemes in message content
       has_link = "http://" in ctx.message.content or "https://" in ctx.message.content


       if not (has_attachment or has_link):
           await ctx.send(f"{ctx.author.mention}, you must attach an image/file or include a link in your check-in.")
           return


   # Minimum word count
   word_min = data.get("word_min", 1)
   if word_min > 1:
       checkInText = " ".join(checkIn)
       if len(checkInText.split()) < word_min:
           await ctx.send(f"{ctx.author.mention}, your check-in must be at least {word_min} words.")
           return


   # Add user to today's check-ins and increment total
   data["dailyCheckedUsers"].append(user_id)
   data["users"][user_id] = data["users"].get(user_id, 0) + 1


   await save_specific_data_to_db(ctx.guild.id, ctx.channel.id, data)  # Persist immediately!
   print(f"INFO: User {user_id} checked in to channel {ctx.channel.id} in guild {ctx.guild.id}.")


   await ctx.send(f"{ctx.author.mention}, you've successfully checked in today in this channel!")






@bot.command()
async def e(ctx):
   """Toggles the requirement for media (image/link) in check-ins. This is channel-specific."""
   data = await get_channel_data(ctx.guild.id, ctx.channel.id)
   if not await is_admin(ctx):
       await ctx.send(f"{ctx.author.mention}, this command is only accessible to admins.")
       return
   data["require_media"] = not data["require_media"]
   status = "no longer" if not data["require_media"] else "now"
   await ctx.send(f"Check-ins in this channel **{status}** require evidence (an image or file).")
   await save_specific_data_to_db(ctx.guild.id, ctx.channel.id, data)  # Save changes
   print(f"INFO: Media requirement for channel {ctx.channel.id} set to {data['require_media']}.")




@bot.command()
async def wl(ctx):
   """Displays the check-in leaderboard (who checked in most). This is channel-specific and aggregates by real name."""
   data = await get_channel_data(ctx.guild.id, ctx.channel.id)


   # Aggregate check-ins by real name
   checkins_by_real_name = {}
   for user_id, checkins in data.get("users", {}).items():
       try:
           user = await bot.fetch_user(user_id)
           # Use userToReal for display names, falling back to discord display name
           real_name = data["userToReal"].get(str(user_id), user.display_name)
       except (discord.NotFound, discord.HTTPException):
           real_name = f"Unknown User ({user_id})"
       checkins_by_real_name[real_name] = checkins_by_real_name.get(real_name, 0) + checkins


   sorted_checkins = sorted(checkins_by_real_name.items(), key=lambda x: x[1], reverse=True)


   embed = discord.Embed(title=f"Check-in Leaderboard for #{ctx.channel.name}", color=discord.Color.green())
   if not sorted_checkins:
       embed.description = "No check-ins recorded yet in this channel!"
   else:
       embed.description = "\n".join(
           f"{i + 1}. **{real_name}**: {checkins} check-in(s)"
           for i, (real_name, checkins) in enumerate(sorted_checkins)
       )
   await ctx.send(embed=embed)




@bot.command()
async def ll(ctx):
   """Displays the missed check-ins leaderboard. This is channel-specific and aggregates by real name."""
   data = await get_channel_data(ctx.guild.id, ctx.channel.id)


   # Aggregate missed check-ins by real name
   missed_by_real_name = {}
   for user_id, missed in data.get("missed_users", {}).items():
       try:
           user = await bot.fetch_user(user_id)
           real_name = data["userToReal"].get(str(user_id), user.display_name)
       except (discord.NotFound, discord.HTTPException):
           real_name = f"Unknown User ({user_id})"
       missed_by_real_name[real_name] = missed_by_real_name.get(real_name, 0) + missed


   sorted_missed = sorted(missed_by_real_name.items(), key=lambda x: x[1], reverse=True)


   embed = discord.Embed(
       title=f"Missed Check-ins Leaderboard for #{ctx.channel.name}",
       color=discord.Color.red()
   )
   if not sorted_missed:
       embed.description = "No missed check-ins recorded yet in this channel!"
   else:
       embed.description = "\n".join(
           f"{i + 1}. **{real_name}**: {missed} missed check-in(s)"
           for i, (real_name, missed) in enumerate(sorted_missed)
       )
   await ctx.send(embed=embed)




@bot.command()
async def t(ctx):
   """Checks who has sent a check-in today and who hasn't. This is channel-specific."""
   data = await get_channel_data(ctx.guild.id, ctx.channel.id)


   checked_users_names = []
   for user_id in data.get("dailyCheckedUsers", []):
       try:
           user = await bot.fetch_user(user_id)
           real_name = data["userToReal"].get(str(user_id), user.display_name)
           checked_users_names.append(real_name)
       except (discord.NotFound, discord.HTTPException):
           checked_users_names.append(f"Unknown User ({user_id})")
   checked_users_list_str = "\n".join(
       checked_users_names) if checked_users_names else "No users checked in today in this channel."


   unchecked_users_names = []
   # Fetch current members from the guild, exclude bots and banned users
   all_members_in_channel = [m for m in ctx.guild.members if not m.bot and m.id not in data.get("banned_users", set())]


   for member in all_members_in_channel:
       if member.id not in data.get("dailyCheckedUsers", []):
           try:
               user_name = data["userToReal"].get(str(member.id), member.display_name)
           except (discord.NotFound, discord.HTTPException):
               user_name = f"Unknown User ({member.id})"
           unchecked_users_names.append(user_name)
   unchecked_list_str = "\n".join(
       unchecked_users_names) if unchecked_users_names else "Everyone checked in today in this channel!"


   await ctx.send(f"**Users who checked in today in #{ctx.channel.name}:**\n{checked_users_list_str}"
                  f"\n\n**Users who have yet to check-in today in #{ctx.channel.name}:**\n{unchecked_list_str}")




@bot.command()
async def n(ctx, *, realNames=None):
   """
   Tracks users and allows mapping Discord user IDs to real names.
   If no arguments, it tracks all current non-banned members. This is channel-specific.
   Prioritizes user IDs for robust mapping.
   """
   data = await get_channel_data(ctx.guild.id, ctx.channel.id)
   if not await is_admin(ctx):
       await ctx.send(f"{ctx.author.mention}, this command is only accessible to admins.")
       return


   if realNames:
       pairs = [pair.strip() for pair in realNames.split(",")]
       for pair in pairs:
           try:
               parts = pair.split(":")
               if len(parts) != 2:
                   await ctx.send(f"Invalid format for '{pair}'. Use 'UserID:RealName' or '@Mention:RealName' format.")
                   return


               identifier, real_name = parts[0].strip(), parts[1].strip()
               target_user_id = None


               if identifier.startswith('<@') and identifier.endswith('>'):
                   try:
                       target_user_id = int(identifier.strip('<@!>'))
                   except ValueError:
                       pass  # Not a valid mention ID
               elif identifier.isdigit():
                   target_user_id = int(identifier)
               else:
                   # If not a mention or ID, try to find by exact display name (less reliable)
                   member = discord.utils.get(ctx.guild.members, display_name=identifier)
                   if member:
                       target_user_id = member.id


               if target_user_id:
                   # Check for existing real name mapping to a different user
                   # This helps prevent accidental overwrites or confusion
                   for existing_user_id_str, existing_real_name in list(
                           data["realPeople"].items()):  # Use list() to iterate over a copy
                       if existing_real_name == real_name and int(existing_user_id_str) != target_user_id:
                           # Warn but still allow the mapping
                           await ctx.send(
                               f"Warning: The real name '{real_name}' is already mapped to <@{existing_user_id_str}>. "
                               f"Mapping <@{target_user_id}> to '{real_name}' will allow both to use this name in leaderboards. "
                               f"Consider using a unique real name for each user if you want distinct leaderboard entries.")
                           break


                   data["userToReal"][str(target_user_id)] = real_name
                   data["realPeople"][str(target_user_id)] = real_name  # Store by ID for consistency
                   print(f"INFO: Mapped {target_user_id} to '{real_name}' in channel {ctx.channel.id}.")
               else:
                   await ctx.send(
                       f"Could not find user for identifier '{identifier}'. Please use a mention, user ID, or exact Discord display name.")
                   return


           except ValueError:
               await ctx.send(f"Invalid format for '{pair}'. Use 'UserID:RealName' or '@Mention:RealName' format.")
               return
   else:
       # If no arguments, track all current non-bot, non-banned members in this channel's context
       # This will refresh the mappings to current display names
       data["userToReal"] = {}
       data["realPeople"] = {}
       for member in ctx.guild.members:
           if not member.bot and member.id not in data["banned_users"]:
               data["userToReal"][str(member.id)] = member.display_name
               data["realPeople"][str(member.id)] = member.display_name
       print(f"INFO: Refreshed all user-to-real name mappings for channel {ctx.channel.id}.")


   user_mappings = "\n".join([f"<@{user_id}> -> {real_name}" for user_id, real_name in data["realPeople"].items()])
   await ctx.send(f"User IDs mapped to real names in #{ctx.channel.name}:\n{user_mappings}")
   await save_specific_data_to_db(ctx.guild.id, ctx.channel.id, data)  # Save changes




@bot.command()
async def a(ctx, user_mention_or_id: str, count: int):
   """
   Adds or removes a certain number of check-ins to a user's count. This is channel-specific.
   Accepts user mention or ID.
   """
   data = await get_channel_data(ctx.guild.id, ctx.channel.id)
   if not await is_admin(ctx):
       await ctx.send(f"{ctx.author.mention}, this command is only accessible to admins.")
       return


   member = None
   user_id = None


   # Try to resolve member from mention or ID
   if user_mention_or_id.startswith('<@') and user_mention_or_id.endswith('>'):
       try:
           user_id = int(user_mention_or_id.strip('<@!>'))
           member = ctx.guild.get_member(user_id)
       except ValueError:
           pass
   elif user_mention_or_id.isdigit():
       user_id = int(user_mention_or_id)
       member = ctx.guild.get_member(user_id)
   else:  # Fallback to searching by name (less reliable)
       member = discord.utils.get(ctx.guild.members, name=user_mention_or_id)
       if member:
           user_id = member.id


   if not member or not user_id:
       await ctx.send(f"User '{user_mention_or_id}' not found in the server. Please use a mention or user ID.")
       return


   data["users"][user_id] = data["users"].get(user_id, 0) + count
   # Ensure check-in count doesn't go below zero, and remove from dict if it becomes 0
   if data["users"][user_id] < 0:
       data["users"][user_id] = 0
   if data["users"][user_id] == 0:
       data["users"].pop(user_id, None)


   action_text = "added to" if count >= 0 else "removed from"
   await ctx.send(
       f"**{abs(count)}** check-in(s) have been {action_text} **{member.display_name}** in #{ctx.channel.name}.")
   await save_specific_data_to_db(ctx.guild.id, ctx.channel.id, data)  # Save changes
   print(f"INFO: {count} check-ins {action_text} {member.display_name} ({user_id}) in channel {ctx.channel.id}.")




@bot.command()
async def w(ctx, min_lim: int):
   """Sets a minimum number of words required in a check-in message. This is channel-specific."""
   data = await get_channel_data(ctx.guild.id, ctx.channel.id)
   if not await is_admin(ctx):
       await ctx.send(f"{ctx.author.mention}, this command is only accessible to admins.")
       return
   if min_lim <= 0:
       await ctx.send(f"{ctx.author.mention}, please enter a positive number (greater than zero).")
       return
   try:
       data["word_min"] = min_lim
       await ctx.send(f"Word minimum set to **{data['word_min']}** words for #{ctx.channel.name}.")
       await save_specific_data_to_db(ctx.guild.id, ctx.channel.id, data)  # Save changes
       print(f"INFO: Word minimum for channel {ctx.channel.id} set to {min_lim}.")
   except ValueError:
       await ctx.send(f"{ctx.author.mention}, please enter a valid number.")




@bot.command()
async def d(ctx, *args: str):
   """
   Manages banned users for check-ins in this channel: ban, unban, or list.
   Accepts user mentions or IDs. This is channel-specific.
   When a user is banned, they are also removed from all leaderboards.
   """
   data = await get_channel_data(ctx.guild.id, ctx.channel.id)
   if not await is_admin(ctx):
       await ctx.send(f"{ctx.author.mention}, this command is only accessible to admins.")
       return
   if not args:
       await ctx.send(
           f"Usage: `c.d @User1 @User2 ...` to ban users, `c.d -u @User1 @User2 ...` to unban, or `c.d -list` to view banned users for #{ctx.channel.name}.")
       return


   if args[0].lower() == "-list":
       if data["banned_users"]:
           banned_mentions = []
           for user_id in data["banned_users"]:
               try:
                   user = await bot.fetch_user(user_id)
                   banned_mentions.append(f"{user.display_name} (<@{user_id}>)")
               except (discord.NotFound, discord.HTTPException):
                   banned_mentions.append(f"Unknown User (<@{user_id}>)")
           banned_list = '\n'.join(banned_mentions)
           await ctx.send(f"**Banned users for #{ctx.channel.name}**:\n{banned_list}")
       else:
           await ctx.send(f"No users are currently banned from checking in this channel.")
       return


   target_user_ids = set()
   # Determine if it's an unban operation based on the first argument
   is_unban = args[0].lower() == "-u"
   users_to_process = args[1:] if is_unban else args


   if not users_to_process:
       await ctx.send("No users specified.")
       return


   for arg in users_to_process:
       user_id = None
       if arg.startswith('<@') and arg.endswith('>'):
           try:
               user_id = int(arg.strip('<@!>'))
           except ValueError:
               pass
       elif arg.isdigit():
           user_id = int(arg)
       else:  # Try to find by name (less reliable)
           member = discord.utils.get(ctx.guild.members, name=arg)
           if member:
               user_id = member.id


       if user_id:
           target_user_ids.add(user_id)
       else:
           await ctx.send(f"Could not find user for argument '{arg}'. Please use a mention or user ID.")
           return


   if is_unban:
       unbanned_ids = target_user_ids.intersection(data["banned_users"])
       if unbanned_ids:
           data["banned_users"] -= unbanned_ids
           unbanned_mentions = [f"<@{uid}>" for uid in unbanned_ids]
           await ctx.send(f"**Unbanned users for #{ctx.channel.name}**:\n{', '.join(unbanned_mentions)}")
           print(f"INFO: Unbanned users {unbanned_ids} from channel {ctx.channel.id}.")
       else:
           await ctx.send("No matching users were banned in this channel.")
   else:  # Ban users
       banning_ids = target_user_ids
       if banning_ids:
           data["banned_users"].update(banning_ids)
           # --- NEW: Remove banned users from leaderboards ---
           for user_id_to_ban in banning_ids:
               data["users"].pop(user_id_to_ban, None)
               data["missed_users"].pop(user_id_to_ban, None)
               print(f"INFO: Removed user {user_id_to_ban} from leaderboards in channel {ctx.channel.id} due to ban.")
           # --- END NEW ---
           banned_mentions = [f"<@{uid}>" for uid in banning_ids]
           await ctx.send(f"**Banned users for #{ctx.channel.name}**:\n{', '.join(banned_mentions)}")
           print(f"INFO: Banned users {banning_ids} from channel {ctx.channel.id}.")
       else:
           await ctx.send("No valid users specified to ban in this channel.")
   await save_specific_data_to_db(ctx.guild.id, ctx.channel.id, data)  # Save changes




@bot.command()
async def tz(ctx, *timezoneset):
   """
   Sets the timezone for the guild's check-in resets. This is a guild-wide setting.
   Use 'list' to see all available timezones.
   """
   guild_settings = await get_guild_settings(ctx.guild.id)
   if not await is_admin(ctx):
       await ctx.send(f"{ctx.author.mention}, this command is only accessible to admins.")
       return
   if not timezoneset:
       await ctx.send(f"Usage: `c.tz list` to view all timezones or `c.tz [timezone]` to set one.")
       return
   if timezoneset[0].lower() == "list":
       tz_list = "\n".join(pytz.all_timezones)
       file_path = "/tmp/timezones.txt"  # Temporary file for sending
       with open(file_path, "w", encoding="utf-8") as file:
           file.write(tz_list)
       try:
           await ctx.send(f"**List of available timezones:**", file=discord.File(file_path))
       except discord.errors.HTTPException as e:
           await ctx.send(f"Could not send timezone list (file too large or other error): {e}")
       finally:
           if os.path.exists(file_path):
               os.remove(file_path)
       return
   if timezoneset:
       tz_string = " ".join(timezoneset)
       if tz_string in pytz.all_timezones:
           guild_settings["timezone"] = tz_string
           await ctx.send(f"Guild timezone set to **{tz_string}**.")
           await save_specific_data_to_db(ctx.guild.id, 0, guild_settings)  # Save guild-wide settings
           print(f"INFO: Guild {ctx.guild.id} timezone set to {tz_string}.")
       else:
           await ctx.send(f"Invalid timezone. Use `c.tz list` to see valid timezones.")




@bot.command()
async def lr(ctx, *leader_input):
   """Resets either the check-in leaderboard (wl) or the missed check-in leaderboard (ll). This is channel-specific."""
   data = await get_channel_data(ctx.guild.id, ctx.channel.id)
   if not await is_admin(ctx):
       await ctx.send(f"{ctx.author.mention}, this command is only accessible to admins.")
       return
   if not leader_input:
       await ctx.send("Please select a leaderboard to reset (either 'wl' or 'll').")
       return
   elif leader_input[0].lower() == "wl":
       data["users"] = {}
       await ctx.send("Check-in leaderboard for this channel has been reset.")
       await save_specific_data_to_db(ctx.guild.id, ctx.channel.id, data)  # Save changes
       print(f"INFO: Check-in leaderboard for channel {ctx.channel.id} reset.")
   elif leader_input[0].lower() == "ll":
       data["missed_users"] = {}
       await ctx.send("Missed check-in leaderboard for this channel has been reset.")
       await save_specific_data_to_db(ctx.guild.id, ctx.channel.id, data)  # Save changes
       print(f"INFO: Missed check-in leaderboard for channel {ctx.channel.id} reset.")
   else:
       await ctx.send(
           "Invalid leaderboard specified. Use 'wl' to reset the check-in leaderboard or 'll' to reset the missed check-in leaderboard.")




@bot.command()
async def r(ctx, resetTime: str):
   """
   Sets the daily reset time for check-ins in HHMMSS format (e.g., 235959 for 11:59:59 PM) for this channel.
   """
   data = await get_channel_data(ctx.guild.id, ctx.channel.id)
   if not await is_admin(ctx):
       await ctx.send(f"{ctx.author.mention}, this command is only accessible to admins.")
       return
   if len(resetTime) != 6 or not resetTime.isdigit():
       await ctx.send(f"{ctx.author.mention}, please enter a 6-digit number (e.g., 235959 for 11:59:59 PM).")
       return


   try:
       hours = int(resetTime[:2])
       minutes = int(resetTime[2:4])
       seconds = int(resetTime[4:])


       if not (0 <= hours <= 23 and 0 <= minutes <= 59 and 0 <= seconds <= 59):
           await ctx.send(f"{ctx.author.mention}, please ensure the time is valid (HH:00-23, MM:00-59, SS:00-59).")
           return


       data["reset_time"] = resetTime
       await ctx.send(f"Reset time for this channel set to **{hours:02}:{minutes:02}:{seconds:02}**.")
       await save_specific_data_to_db(ctx.guild.id, ctx.channel.id, data)  # Save changes
       print(f"INFO: Reset time for channel {ctx.channel.id} set to {resetTime}.")
   except ValueError:
       await ctx.send(f"{ctx.author.mention}, invalid time format. Please use a 6-digit number.")




@bot.command()
async def cr(ctx):
   """Shows the current daily reset time for this channel and the guild's timezone."""
   data = await get_channel_data(ctx.guild.id, ctx.channel.id)
   reset_time_str = data.get("reset_time")
   guild_settings = await get_guild_settings(ctx.guild.id)
   timezone_str = guild_settings.get("timezone", "America/Los_Angeles")  # Provide default


   if not reset_time_str:
       await ctx.send(f"No reset time configured for this channel. The guild timezone is **{timezone_str}**.")
       return


   try:
       reset_hour = int(reset_time_str[:2])
       reset_minute = int(reset_time_str[2:4])
       reset_second = int(reset_time_str[4:])


       period = "AM"
       reset_hour_12 = reset_hour
       if reset_hour == 0:
           reset_hour_12 = 12
       elif reset_hour == 12:
           period = "PM"
       elif reset_hour > 12:
           reset_hour_12 = reset_hour - 12
           period = "PM"


       formatted_reset_time = f"{reset_hour_12:02}:{reset_minute:02}:{reset_second:02} {period}"
       await ctx.send(f"The reset time for this channel is **{formatted_reset_time}** ({timezone_str}).")


   except ValueError:
       await ctx.send(
           f"Error: Invalid reset time format stored for this channel ({reset_time_str}). Please contact an admin to fix it using `c.r`.")
   except Exception as e:
       await ctx.send(f"An unexpected error occurred: {e}")


MAX_EMBED_FIELD_LENGTH = 1024


# @bot.command()
# async def sum(ctx, *, time_range_str: str = None):
#     """
#     Summarizes the check-ins received for a specific time range: a date (MM-DD),
#     'week' (last 7 days), or 'month' (last 30 days).
#
#     If no time range is provided, it summarizes check-ins since the last daily reset.
#     """
#     data = await get_channel_data(ctx.guild.id, ctx.channel.id)
#     guild_settings = await get_guild_settings(ctx.guild.id)
#     timezone_str = guild_settings.get("timezone", "America/Los_Angeles")
#     reset_hour = guild_settings.get("reset_hour", 0)
#     reset_minute = guild_settings.get("reset_minute", 0)
#
#     try:
#         guild_tz = pytz.timezone(timezone_str)
#     except pytz.exceptions.UnknownTimeZoneError:
#         guild_tz = pytz.timezone("America/Los_Angeles")
#
#     google_api_key = os.getenv("GOOGLE_API_KEY")
#     if not google_api_key:
#         await ctx.send("ERROR: GOOGLE_API_KEY environment variable not set. Summary command will not work.")
#         return
#
#     if ctx.guild is None:
#         await ctx.send("This command can only be used in a server channel.")
#         return
#
#     await ctx.typing()
#
#     start_time_utc = None
#     end_time_utc = datetime.now(pytz.utc)
#     display_range_str = ""
#     checkin_command_string = f"{ctx.prefix}c"
#
#     # --- Time Range Parsing ---
#     if time_range_str:
#         time_range_str = time_range_str.strip()
#
#         if time_range_str.lower() in ["week", "1week", "7d", "7days"]:
#             start_time_utc = end_time_utc - timedelta(days=7)
#             display_range_str = "the last week"
#
#         elif time_range_str.lower() in ["month", "1month", "30d", "30days"]:
#             start_time_utc = end_time_utc - timedelta(days=30)
#             display_range_str = "the last month"
#
#         else:
#             # Try MM-DD or M-D
#             match_mm_dd = re.match(r'^\s*(\d{1,2})-(\d{1,2})\s*$', time_range_str)
#             if match_mm_dd:
#                 try:
#                     month = int(match_mm_dd.group(1))
#                     day = int(match_mm_dd.group(2))
#                     year = datetime.now().year
#                     target_date_local = datetime(year, month, day).date()
#                 except ValueError:
#                     await ctx.send(f"Invalid date '{time_range_str}'. Please use `MM-DD` with a valid month/day.")
#                     return
#             else:
#                 # Fallback to dateutil parser
#                 try:
#                     parsed_datetime_naive = parser.parse(time_range_str)
#                     target_date_local = parsed_datetime_naive.date()
#                 except Exception:
#                     await ctx.send(
#                         f"Could not understand the time range '{time_range_str}'. Please use a format like `MM-DD`, `week`, or `month`."
#                     )
#                     return
#
#             # Convert to UTC range
#             start_of_target_day_naive = datetime.combine(target_date_local, time.min)
#             end_of_target_day_naive = datetime.combine(target_date_local, time.max)
#
#             start_time_utc = guild_tz.localize(start_of_target_day_naive).astimezone(pytz.utc)
#             end_time_utc = guild_tz.localize(end_of_target_day_naive).astimezone(pytz.utc)
#
#             current_guild_date = datetime.now(guild_tz).date()
#             if target_date_local == current_guild_date:
#                 display_range_str = "Today"
#             else:
#                 display_range_str = target_date_local.strftime('%Y-%m-%d')
#
#     else:
#         # No time range provided — since last reset
#         last_reset_time_utc = data.get("last_reset_time")
#         if last_reset_time_utc and isinstance(last_reset_time_utc, datetime) and last_reset_time_utc.tzinfo:
#             start_time_utc = last_reset_time_utc
#             display_range_str = "Since Last Reset"
#         else:
#             now_guild_tz = datetime.now(guild_tz)
#             current_day_start_local = now_guild_tz.replace(hour=reset_hour, minute=reset_minute, second=0, microsecond=0)
#             if now_guild_tz < current_day_start_local:
#                 current_day_start_local -= timedelta(days=1)
#
#             start_of_current_day_utc = guild_tz.localize(current_day_start_local).astimezone(pytz.utc)
#             start_time_utc = start_of_current_day_utc
#             display_range_str = "Today (Start of Day, based on reset time)"
#
#     # --- Build Gemini Prompt ---
#     content_for_gemini_prompt = [
#         f"Provide a concise summary of the following daily check-ins from Discord users. "
#         f"Start with an 'Overall Summary' (1-3 bullet points on key themes). "
#         f"Then, add a section titled 'Individual Contributions'. "
#         f"For each user, provide a concise summary of their specific check-in, detailing their main activity/update. "
#         f"[... instructions truncated for brevity ...]\n"
#         f"Here are the check-ins:\n"
#     ]
#
#     try:
#         async for message in ctx.channel.history(after=start_time_utc, before=end_time_utc, limit=500):
#             if not message.author.bot and (message.content or message.attachments):
#                 cleaned_content = message.content.strip()
#                 if cleaned_content.lower().startswith(checkin_command_string.lower()):
#                     actual_checkin_content = cleaned_content[len(checkin_command_string):].strip()
#
#                     if not actual_checkin_content and message.attachments:
#                         has_image_for_gemini = False
#                         for attachment in message.attachments:
#                             if 'image' in attachment.content_type:
#                                 try:
#                                     image_bytes = await attachment.read()
#                                     pil_image = Image.open(BytesIO(image_bytes))
#                                     content_for_gemini_prompt.append(
#                                         f"\n--- Check-in by {message.author.display_name} ({message.created_at.strftime('%H:%M')}):\n"
#                                         f"[No text provided in check-in message. Summarize based on image.]"
#                                     )
#                                     content_for_gemini_prompt.append(pil_image)
#                                     has_image_for_gemini = True
#                                     break
#                                 except Exception:
#                                     content_for_gemini_prompt.append(
#                                         f"[Error loading image for image-only check-in: {attachment.filename}]")
#                         if not has_image_for_gemini:
#                             content_for_gemini_prompt.append(
#                                 f"\n--- Check-in by {message.author.display_name} ({message.created_at.strftime('%H:%M')}):\n"
#                                 f"[No text or valid image provided in check-in message, skipping.]"
#                             )
#                     elif actual_checkin_content:
#                         content_for_gemini_prompt.append(
#                             f"\n--- Check-in by {message.author.display_name} ({message.created_at.strftime('%H:%M')}):\n"
#                             f"{actual_checkin_content}"
#                         )
#                         has_image_for_gemini = False
#                         if message.attachments:
#                             for attachment in message.attachments:
#                                 if 'image' in attachment.content_type:
#                                     try:
#                                         image_bytes = await attachment.read()
#                                         pil_image = Image.open(BytesIO(image_bytes))
#                                         content_for_gemini_prompt.append(pil_image)
#                                         has_image_for_gemini = True
#                                         break
#                                     except Exception:
#                                         content_for_gemini_prompt.append(
#                                             f"[Error loading image: {attachment.filename}]")
#                         if not has_image_for_gemini:
#                             content_for_gemini_prompt.append("[No image provided with check-in.]")
#     except discord.errors.Forbidden:
#         await ctx.send("I don't have permission to read message history in this channel.")
#         return
#     except discord.errors.HTTPException as e:
#         await ctx.send(f"An error occurred while fetching messages: {e}")
#         return
#
#     if len(content_for_gemini_prompt) == 1:
#         await ctx.send(
#             f"No check-in messages (text or image) found for **{display_range_str}** in this channel to summarize.")
#         return
#
#     try:
#         genai.configure(api_key=google_api_key)
#         model = genai.GenerativeModel('gemini-1.5-flash')
#         response = model.generate_content(content_for_gemini_prompt)
#         summary_text = response.text.strip()
#         if len(summary_text) > MAX_EMBED_FIELD_LENGTH:
#             summary_text = summary_text[:MAX_EMBED_FIELD_LENGTH - 3] + "..."
#
#         embed = discord.Embed(
#             title=f"Check-in Summary for {display_range_str} in #{ctx.channel.name}",
#             description=summary_text,
#             color=discord.Color.purple()
#         )
#         embed.set_footer(
#             text=f"Summary generated by Google Gemini on {datetime.now(pytz.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
#         await ctx.send(embed=embed)
#     except Exception as e:
#         await ctx.send(f"An error occurred with the Google Gemini API: {e}")
#
#
#
# @bot.command()
# async def topic(ctx, *, topic_query: str):
#     """
#     Summarizes check-ins related to a specific topic using Google's Gemini model,
#     analyzing both text and image content. It filters messages based on the topic.
#     Example usage:
#     `c.topic bot development`
#     `c.topic "new features"`
#     """
#     data = await get_channel_data(ctx.guild.id, ctx.channel.id)
#
#     guild_settings = await get_guild_settings(ctx.guild.id)
#     timezone_str = guild_settings.get("timezone", "America/Los_Angeles")
#     try:
#         guild_tz = pytz.timezone(timezone_str)
#     except pytz.exceptions.UnknownTimeZoneError:
#         guild_tz = pytz.timezone("America/Los_Angeles")
#
#     google_api_key = os.getenv("GOOGLE_API_KEY")
#     if not google_api_key:
#         await ctx.send("ERROR: GOOGLE_API_KEY environment variable not set. Topic command will not work.")
#         return
#
#     if ctx.guild is None:
#         await ctx.send("This command can only be used in a server channel.")
#         return
#
#     if not topic_query:
#         await ctx.send("Please provide a topic to summarize. Example: `c.topic project updates`")
#         return
#
#     await ctx.typing()
#
#     # Define the time range for fetching messages.
#     # We'll fetch from the last_reset_time, or 7 days ago if no last_reset_time.
#     start_time_utc = data.get("last_reset_time")
#     if not start_time_utc or not isinstance(start_time_utc, datetime) or not start_time_utc.tzinfo:
#         start_time_utc = datetime.now(pytz.utc) - timedelta(days=7)  # Default lookback period
#         print(f"DEBUG: No last_reset_time found for topic command, defaulting to 7 days ago: {start_time_utc}")
#
#     end_time_utc = datetime.now(pytz.utc)
#     checkin_command_string = f"{ctx.prefix}c"
#
#     # --- Initial prompt without detailed image instructions ---
#     initial_gemini_prompt_base = [
#         f"**Task:** Provide a concise summary of Discord check-ins that are specifically related to the topic: '{topic_query}'. "
#         f"**Instructions:**\n"
#         f"1.  **Filter Strictly:** Only include information from check-ins that are directly relevant to '{topic_query}'. Ignore unrelated check-ins. If no check-ins are relevant, state that clearly.\n"
#         f"2.  **Overall Topic Summary:** Begin with an 'Overall Topic Summary' (1-2 bullet points on the main points related to '{topic_query}' found across all relevant check-ins).\n"
#         f"3.  **Individual Contributions (Relevant Only):** For *each user* who submitted a relevant check-in, provide a concise summary of their specific contribution to '{topic_query}'.\n"
#         f"4.  **Image Analysis (Simplified for initial filter):** Briefly mention if an image was present (e.g., 'Image: Present' or 'Image: None provided'). Do not analyze content in detail at this stage, only relevance to the topic.\n"
#         f"5.  **Format:** Use bullet points for the overall topic summary and distinct, nested bullet points for each user's relevant contribution. Ensure all content relates to '{topic_query}'.\n"
#         f"\nHere are the check-ins (only use those relevant to '{topic_query}'):\n"
#     ]
#     # --- Detailed image analysis instructions (to be added conditionally) ---
#     detailed_image_instructions = (
#         f"**Crucial Instructions for Image Analysis (VERY IMPORTANT):**\n"
#         f"1.  **Bot Development/Testing & Topic:** If a user checks in about **developing, debugging, or testing a Discord bot** related to '{topic_query}', AND provides an image that appears to be a **screenshot of Discord content (such as bot outputs, embeds, summaries, or command results)**, you **must consider this image highly relevant** to their stated activity. Describe what the image shows (e.g., '✅ The image displays the bot's summary output, confirming the user's testing of the summary function related to the topic.') and how it directly relates to their check-in. \n"
#         f"2.  **Measurements & Values & Topic:** If an image contains **clear numerical measurements or values on instruments like beakers, gauges, scales, or thermometers**, and these values are relevant to the user's check-in text *and the topic* (e.g., 'measured x', 'experiment result', 'tracked progress'), you **must precisely read and state the exact value displayed**. For example, if a beaker is shown, state '✅ The image shows a beaker with approximately [X] mL of liquid.' Pay close attention to scale markings and the liquid's meniscus for accuracy, interpolating between marked values if necessary. \n"
#         f"3.  **Other Irrelevant Images:** If an image is otherwise irrelevant or a generic photo unrelated to the user's specific text description *or the topic*, explicitly state '❌ Irrelevant (no direct support for topic-related text)'. Avoid describing the irrelevant image content.\n"
#         f"4.  **No Image:** If no image was provided, explicitly state 'Image: None provided'.\n"
#         f"5.  **Image Only Check-in & Topic:** If a check-in consists *only* of an image with no text, and the image itself clearly relates to '{topic_query}', summarize the content of the image as the user's check-in. State '✅ [Detailed description of image content as the check-in, clearly related to the topic. This was an image-only check-in.]'. If the image is not clearly related to the topic, ignore this check-in.\n"
#     )
#
#     content_for_gemini_messages = []
#     has_checkin_messages_to_process = False  # Flag to indicate if any check-in messages were found
#
#     try:
#         async for message in ctx.channel.history(after=start_time_utc, before=end_time_utc, limit=500):
#             if not message.author.bot and (message.content or message.attachments):
#                 cleaned_content = message.content.strip()
#                 if cleaned_content.lower().startswith(checkin_command_string.lower()):
#                     actual_checkin_content = cleaned_content[len(checkin_command_string):].strip()
#
#                     checkin_entry_parts = [
#                         f"\n--- Check-in by {message.author.display_name} ({message.created_at.strftime('%H:%M')}):\n"
#                     ]
#                     if actual_checkin_content:
#                         checkin_entry_parts.append(actual_checkin_content)
#                     else:
#                         checkin_entry_parts.append("[No text provided in check-in message.]")
#
#                     found_image_for_checkin = False
#                     if message.attachments:
#                         for attachment in message.attachments:
#                             if 'image' in attachment.content_type:
#                                 try:
#                                     image_bytes = await attachment.read()
#                                     pil_image = Image.open(BytesIO(image_bytes))
#                                     checkin_entry_parts.append(pil_image)
#                                     found_image_for_checkin = True
#                                     print(
#                                         f"DEBUG: Found and prepared image for {message.author.display_name}'s check-in (topic).")
#                                     break
#                                 except Exception as e:
#                                     checkin_entry_parts.append(f"[Error loading image: {attachment.filename}]")
#                                     print(
#                                         f"ERROR: Could not load image for Gemini for {message.author.display_name}'s check-in ({attachment.filename}): {e}")
#
#                     if not found_image_for_checkin:
#                         checkin_entry_parts.append("[No image provided with check-in.]")
#
#                     content_for_gemini_messages.extend(checkin_entry_parts)
#                     has_checkin_messages_to_process = True  # At least one check-in command was found
#
#     except discord.errors.Forbidden:
#         await ctx.send("I don't have permission to read message history in this channel.")
#         return
#     except discord.errors.HTTPException as e:
#         await ctx.send(f"An error occurred while fetching messages: {e}")
#         return
#
#     if not has_checkin_messages_to_process:
#         await ctx.send(
#             f"No check-in messages (text or image) found in the last 7 days for summarization in this channel.")
#         return
#
#     # Construct the final prompt based on whether check-in messages were found
#     final_gemini_prompt_content = []
#
#     # Always start with the base prompt.
#     final_gemini_prompt_content.extend(initial_gemini_prompt_base)
#
#     # Conditionally inject detailed image instructions IF check-in messages were found.
#     if has_checkin_messages_to_process:
#         # Find the placeholder for simplified image analysis in the base prompt
#         # and replace it with the detailed instructions.
#         found_placeholder_for_image_inst = False
#         for i, item in enumerate(final_gemini_prompt_content):
#             if isinstance(item, str) and "Image Analysis (Simplified for initial filter):" in item:
#                 final_gemini_prompt_content[i] = detailed_image_instructions
#                 found_placeholder_for_image_inst = True
#                 break
#         # Fallback in case the exact placeholder string changes in initial_gemini_prompt_base
#         if not found_placeholder_for_image_inst:
#             final_gemini_prompt_content.insert(len(initial_gemini_prompt_base) - 1, detailed_image_instructions)
#
#     # Add the collected check-in messages (text and images) to the final prompt.
#     final_gemini_prompt_content.extend(content_for_gemini_messages)
#
#     try:
#         genai.configure(api_key=google_api_key)
#         model = genai.GenerativeModel('gemini-1.5-flash')
#
#         # Function to call Gemini's generate_content in a separate thread
#         def _call_gemini_generate_content(prompt_content):
#             return model.generate_content(prompt_content)
#
#         # Execute the Gemini API call in a separate thread to prevent blocking the Discord event loop
#         response = await asyncio.to_thread(_call_gemini_generate_content, final_gemini_prompt_content)
#
#         summary_text = response.text.strip()
#
#         if len(summary_text) > MAX_EMBED_FIELD_LENGTH:
#             summary_text = summary_text[:MAX_EMBED_FIELD_LENGTH - 3] + "..."
#             print(f"WARNING: Gemini topic summary truncated to {MAX_EMBED_FIELD_LENGTH} characters.")
#
#         # Check if Gemini indicates no relevant check-ins
#         if "no check-ins directly related to" in summary_text.lower() or \
#                 "no relevant check-ins were found" in summary_text.lower() or \
#                 len(summary_text) < 50:  # Arbitrary length for "too short" to indicate no content
#             summary_text = f"No check-ins directly related to '{topic_query}' were found or summarized in this channel."
#
#         embed = discord.Embed(
#             title=f"Topic Summary: '{topic_query}' in #{ctx.channel.name}",
#             description=summary_text,
#             color=discord.Color.green()
#         )
#         embed.set_footer(
#             text=f"Summary generated by Google Gemini on {datetime.now(pytz.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
#
#         await ctx.send(embed=embed)
#         print(
#             f"INFO: Successfully generated and sent topic summary for channel {ctx.channel.id} on topic '{topic_query}' using Google Gemini (multimodal).")
#
#     except Exception as e:
#         await ctx.send(f"An error occurred with the Google Gemini API while summarizing for topic '{topic_query}': {e}")
#         print(f"ERROR: Google Gemini API Error (multimodal, topic command): {e}")


# Existing resetTime task (truncated for brevity, assume it's fully present)
@tasks.loop(seconds=1)
async def resetTime():
    """
    Checks every second if it's time to reset check-ins for each channel,
    using reset time and timezone loaded from Postgres. Only sends the
    summary once per day per channel, and persists last_reset_time in Postgres.
    """
    now_utc = datetime.now(pytz.utc)

    for guild_id, guild_data_entry in list(guild_channel_data_cache.items()):
        guild_settings = guild_data_entry.get(0, {})

        timezone_str = guild_settings.get("timezone", "America/Los_Angeles")
        try:
            guild_tz = pytz.timezone(timezone_str)
            now_guild_tz = now_utc.astimezone(guild_tz)
        except pytz.exceptions.UnknownTimeZoneError:
            print(
                f"WARNING: Invalid timezone '{timezone_str}' for guild {guild_id}. Defaulting to America/Los_Angeles.")
            guild_settings["timezone"] = "America/Los_Angeles"  # Correct the timezone in cache
            await save_specific_data_to_db(guild_id, 0, guild_settings)  # Persist corrected timezone
            guild_tz = pytz.timezone("America/Los_Angeles")
            now_guild_tz = now_utc.astimezone(guild_tz)

        formatted_date = now_guild_tz.strftime("%Y-%m-%d")

        for channel_id, channel_data in list(guild_data_entry.items()):
            if channel_id == 0:
                continue

            reset_time_str = channel_data.get("reset_time")
            if not reset_time_str:
                continue

            try:
                reset_hour = int(reset_time_str[:2])
                reset_minute = int(reset_time_str[2:4])
                reset_second = int(reset_time_str[4:])
            except Exception as e:
                print(
                    f"ERROR: Invalid reset_time_str '{reset_time_str}' for channel {channel_id} in guild {guild_id}: {e}")
                continue

            last_reset_time_utc = channel_data.get("last_reset_time")
            last_reset_time_guild_tz = None
            if isinstance(last_reset_time_utc, datetime) and last_reset_time_utc.tzinfo:
                last_reset_time_guild_tz = last_reset_time_utc.astimezone(guild_tz)
            elif isinstance(last_reset_time_utc, datetime):
                last_reset_time_guild_tz = last_reset_time_utc.replace(tzinfo=pytz.utc).astimezone(guild_tz)

            already_reset_today = (
                    last_reset_time_guild_tz is not None
                    and last_reset_time_guild_tz.date() == now_guild_tz.date()
                    and last_reset_time_guild_tz.hour == reset_hour
                    and last_reset_time_guild_tz.minute == reset_minute
            )

            if (
                    now_guild_tz.hour == reset_hour and
                    now_guild_tz.minute == reset_minute and
                    now_guild_tz.second == reset_second
            ):
                if not already_reset_today:
                    print(f"INFO: Reset condition met for channel {channel_id} in guild {guild_id}. Triggering reset.")
                    await _perform_channel_reset(
                        guild_id, channel_id, channel_data, now_guild_tz, formatted_date
                    )
                else:
                    print(
                        f"INFO: Channel {channel_id} in guild {guild_id} at {now_guild_tz.strftime('%H:%M:%S')} already reset today. Skipping.")
            else:
                pass

@tasks.loop(seconds=1)
async def resetTime():
   """
   Checks every second if it's time to reset check-ins for each channel,
   using reset time and timezone loaded from Postgres. Only sends the
   summary once per day per channel, and persists last_reset_time in Postgres.
   """
   now_utc = datetime.now(pytz.utc)


   # Iterate over a copy of items to avoid "dictionary changed size during iteration" errors
   # and to ensure we process all currently loaded guilds/channels.
   for guild_id, guild_data_entry in list(guild_channel_data_cache.items()):
       guild_settings = guild_data_entry.get(0, {})  # Get guild settings from cache


       timezone_str = guild_settings.get("timezone", "America/Los_Angeles")
       try:
           guild_tz = pytz.timezone(timezone_str)
           now_guild_tz = now_utc.astimezone(guild_tz)
       except pytz.exceptions.UnknownTimeZoneError:
           print(
               f"WARNING: Invalid timezone '{timezone_str}' for guild {guild_id}. Defaulting to America/Los_Angeles.")
           guild_settings["timezone"] = "America/Los_Angeles"  # Correct the timezone in cache
           await save_specific_data_to_db(guild_id, 0, guild_settings)  # Persist corrected timezone
           guild_tz = pytz.timezone("America/Los_Angeles")
           now_guild_tz = now_utc.astimezone(guild_tz)


       formatted_date = now_guild_tz.strftime("%Y-%m-%d")


       # Iterate over channel data within the guild
       for channel_id, channel_data in list(guild_data_entry.items()):
           if channel_id == 0:  # Skip guild settings entry
               continue


           reset_time_str = channel_data.get("reset_time")
           if not reset_time_str:
               continue


           try:
               reset_hour = int(reset_time_str[:2])
               reset_minute = int(reset_time_str[2:4])
               reset_second = int(reset_time_str[4:])
           except Exception as e:
               print(
                   f"ERROR: Invalid reset_time_str '{reset_time_str}' for channel {channel_id} in guild {guild_id}: {e}")
               continue


           last_reset_time_utc = channel_data.get("last_reset_time")
           last_reset_time_guild_tz = None
           if isinstance(last_reset_time_utc, datetime) and last_reset_time_utc.tzinfo:
               last_reset_time_guild_tz = last_reset_time_utc.astimezone(guild_tz)
           elif isinstance(last_reset_time_utc, datetime):
               last_reset_time_guild_tz = last_reset_time_utc.replace(tzinfo=pytz.utc).astimezone(guild_tz)


           already_reset_today = (
                   last_reset_time_guild_tz is not None
                   and last_reset_time_guild_tz.date() == now_guild_tz.date()
                   and last_reset_time_guild_tz.hour == reset_hour
                   and last_reset_time_guild_tz.minute == reset_minute
           )


           if (
                   now_guild_tz.hour == reset_hour and
                   now_guild_tz.minute == reset_minute and
                   now_guild_tz.second == reset_second
           ):
               if not already_reset_today:
                   print(f"INFO: Reset condition met for channel {channel_id} in guild {guild_id}. Triggering reset.")
                   await _perform_channel_reset(
                       guild_id, channel_id, channel_data, now_guild_tz, formatted_date
                   )
               else:
                   print(
                       f"INFO: Channel {channel_id} in guild {guild_id} at {now_guild_tz.strftime('%H:%M:%S')} already reset today. Skipping.")
           else:
               pass


# async def _perform_channel_reset(guild_id, channel_id, channel_data, now_guild_tz, formatted_date):
#     """
#     Performs the reset for a channel: calculates and posts leaderboards, resets daily check-in lists,
#     and persists changes to Postgres. Now includes Gemini summary for the period since the last reset,
#     analyzing both text and image content.
#     """
#     print(
#         f"INFO: Starting reset process for channel {channel_id} in guild {guild_id} at {now_guild_tz.strftime('%Y-%m-%d %H:%M:%S')}.")
#
#     google_api_key = os.getenv("GOOGLE_API_KEY")
#     gemini_summary_text = ""
#
#     if not google_api_key:
#         print(
#             f"ERROR: GOOGLE_API_KEY environment variable not set. Cannot generate Gemini summary for channel {channel_id}.")
#         gemini_summary_text = "Automatic summary failed: Google API Key not configured."
#     else:
#         genai.configure(api_key=google_api_key)
#         gemini_model = genai.GenerativeModel('gemini-1.5-flash')
#
#     start_time_for_summary_fetch_utc = channel_data.get("last_reset_time")
#     if not start_time_for_summary_fetch_utc or not isinstance(start_time_for_summary_fetch_utc,
#                                                               datetime) or not start_time_for_summary_fetch_utc.tzinfo:
#         guild_settings = await get_guild_settings(guild_id)
#         timezone_str = guild_settings.get("timezone", "America/Los_Angeles")
#         try:
#             guild_tz = pytz.timezone(timezone_str)
#         except pytz.exceptions.UnknownTimeZoneError:
#             guild_tz = pytz.timezone("America/Los_Angeles")
#
#         start_of_day_guild_tz = now_guild_tz.replace(hour=0, minute=0, second=0, microsecond=0)
#         start_time_for_summary_fetch_utc = start_of_day_guild_tz.astimezone(pytz.utc)
#         print(f"DEBUG: Using fallback start time for summary fetch: {start_time_for_summary_fetch_utc}")
#     else:
#         print(f"DEBUG: Using last_reset_time for summary fetch: {start_time_for_summary_fetch_utc}")
#
#     channel = bot.get_channel(channel_id)
#     if not channel:
#         print(
#             f"WARNING: Channel {channel_id} not found or inaccessible in guild {guild_id}. Cannot send summary or fetch messages.")
#         gemini_summary_text = "Automatic summary failed: Channel not found or accessible."
#     else:
#         # --- MODIFIED PROMPT INSTRUCTION FOR STRUCTURED OUTPUT AND ENHANCED IMAGE RELEVANCE ---
#         content_for_gemini_prompt = [
#             f"Provide a concise summary of the following daily check-ins from Discord users. "
#             f"Start with an 'Overall Summary' (1-3 bullet points on key themes). "
#             f"Then, add a section titled 'Individual Contributions'. "
#             f"For each user, provide a concise summary of their specific check-in, detailing their main activity/update from both text and image (if provided). "
#             f"**Crucial Instruction for Image Relevance & Detail:**\n"
#             f"1.  **Bot Development/Testing Images:** If a user checks in about **developing, debugging, or testing a Discord bot**, and provides an image that appears to be a **screenshot of Discord content (such as bot outputs, embeds, summaries, or command results)**, you **must consider this image highly relevant** to their stated activity. Describe what the image shows (e.g., 'The image displays the bot's summary output, confirming the user's testing of the summary function.') and how it directly relates to their check-in. "
#             f"2.  **Measurements & Values:** If an image contains **clear numerical measurements or values on instruments like beakers, gauges, scales, or thermometers**, and these values are relevant to the user's check-in text (e.g., 'measured x', 'experiment result', 'tracked progress'), you **must precisely read and state the exact value displayed**. For example, if a beaker is shown, state 'The image shows a beaker with approximately [X] mL of liquid.' Pay close attention to scale markings and the liquid's meniscus for accuracy, interpolating between marked values if necessary. "
#             f"3.  **Other Irrelevant Images:** If an image is otherwise irrelevant or a generic photo unrelated to the user's specific text description, explicitly state 'Image: Irrelevant'. "
#             f"4.  **No Image:** If no image was provided, explicitly state 'Image: None provided'. "
#             f"Use bullet points for the overall summary and distinct, nested bullet points for each user's contribution. "
#             f"Example format:\n"
#             f"**Overall Summary:**\n"
#             f"- [Overall theme 1]\n"
#             f"- [Overall theme 2]\n"
#             f"\n"
#             f"**Individual Contributions:**\n"
#             f"- **[User A]**: [Summary of User A's check-in. Image: Relevant description / Irrelevant / None provided.]\n"
#             f"- **[User B]**: [Summary of User B's check-in. Image: Relevant description / Irrelevant / None provided.]\n"
#             f"\nHere are the check-ins:\n"
#         ]
#         # --- END MODIFIED PROMPT INSTRUCTION ---
#
#         checkin_command_string = "c.c"
#
#         try:
#             async for message in channel.history(after=start_time_for_summary_fetch_utc, limit=500):
#                 if not message.author.bot and (message.content or message.attachments):
#                     cleaned_content = message.content.strip()
#                     if cleaned_content.lower().startswith(checkin_command_string.lower()):
#                         actual_checkin_content = cleaned_content[len(checkin_command_string):].strip()
#
#                         if actual_checkin_content or message.attachments:
#                             content_for_gemini_prompt.append(
#                                 f"\n--- Check-in by {message.author.display_name} ({message.created_at.strftime('%H:%M')}):\n"
#                             )
#                             if actual_checkin_content:
#                                 content_for_gemini_prompt.append(actual_checkin_content)
#                             else:
#                                 content_for_gemini_prompt.append("[No text provided in check-in message.]")
#
#                             has_image_for_gemini = False
#                             if message.attachments:
#                                 for attachment in message.attachments:
#                                     if 'image' in attachment.content_type:
#                                         try:
#                                             image_bytes = await attachment.read()
#                                             pil_image = Image.open(BytesIO(image_bytes))
#                                             content_for_gemini_prompt.append(pil_image)
#                                             has_image_for_gemini = True
#                                             print(
#                                                 f"DEBUG: Found and prepared image for {message.author.display_name}'s check-in for Gemini.")
#                                             break
#                                         except Exception as e:
#                                             content_for_gemini_prompt.append(
#                                                 f"[Error loading image: {attachment.filename}]")
#                                             print(
#                                                 f"ERROR: Could not load image for Gemini for {message.author.display_name}'s check-in ({attachment.filename}): {e}")
#
#                             if not has_image_for_gemini:
#                                 content_for_gemini_prompt.append("[No image provided with check-in.]")
#
#             if len(content_for_gemini_prompt) == 1:
#                 gemini_summary_text = "No check-in messages (text or image) found since last reset for summarization."
#             elif not google_api_key:
#                 gemini_summary_text = "Automatic summary skipped: Google API Key not configured."
#             else:
#                 try:
#                     gemini_response = gemini_model.generate_content(content_for_gemini_prompt)
#                     gemini_summary_text = gemini_response.text.strip()
#
#                     if len(gemini_summary_text) > MAX_EMBED_FIELD_LENGTH:
#                         gemini_summary_text = gemini_summary_text[:MAX_EMBED_FIELD_LENGTH - 3] + "..."
#                         print(
#                             f"WARNING: Gemini summary truncated to {MAX_EMBED_FIELD_LENGTH} characters for channel {channel_id}.")
#
#                 except Exception as e:
#                     gemini_summary_text = f"Automatic summary failed (Gemini API Error): {e}"
#                     print(f"ERROR: Gemini API Error during automatic summary for channel {channel_id}: {e}")
#
#         except discord.errors.Forbidden:
#             gemini_summary_text = "Automatic summary failed: Bot does not have permission to read message history."
#             print(f"ERROR: Missing permissions to read history in channel {channel_id} of guild {guild_id}.")
#         except discord.errors.HTTPException as e:
#             gemini_summary_text = f"Automatic summary failed (Discord API Error): {e}"
#             print(f"ERROR: Discord API Error fetching messages for summary in channel {channel_id}: {e}")
#         except Exception as e:
#             gemini_summary_text = f"Automatic summary failed (Unexpected Error): {e}"
#             print(f"ERROR: Unexpected error during message fetching for summary in channel {channel_id}: {e}")
#
#     # --- Save the current reset time to the channel data ---
#     channel_data["last_reset_time"] = now_guild_tz.astimezone(pytz.utc)
#
#     # --- Prepare list of users who checked in for the day that just ended ---
#     checked_users_names = []
#     for user_id in channel_data.get("dailyCheckedUsers", []):
#         try:
#             user = await bot.fetch_user(user_id)
#             real_name = channel_data.get("userToReal", {}).get(str(user_id), user.display_name)
#             checked_users_names.append(real_name)
#         except (discord.NotFound, discord.HTTPException):
#             checked_users_names.append(f"Unknown User ({user_id})")
#     checked_users_list_str = "\n".join(checked_users_names) if checked_users_names else "No users checked in today."
#     print(f"DEBUG: Checked users for summary: {checked_users_names}")
#
#     # --- Find users who missed check-in for the day that just ended ---
#     unchecked_user_ids = []
#     unchecked_users_names = []
#     guild = bot.get_guild(guild_id)
#     if guild:
#         for member in guild.members:
#             if not member.bot and member.id not in channel_data.get("banned_users",
#                                                                     set()) and member.id not in channel_data.get(
#                     "dailyCheckedUsers", []):
#                 try:
#                     real_name = channel_data.get("userToReal", {}).get(str(member.id), member.display_name)
#                 except (discord.NotFound, discord.HTTPException):
#                     real_name = f"Unknown User ({member.id})"
#                 unchecked_users_names.append(real_name)
#                 unchecked_user_ids.append(member.id)
#     unchecked_list_str = "\n".join(unchecked_users_names) if unchecked_users_names else "Everyone checked in today!"
#     print(f"DEBUG: Unchecked users for summary: {unchecked_users_names}")
#
#     # --- Update missed_users dict (for persistent tracking of consecutive misses) ---
#     missed_users = channel_data.get("missed_users", {})
#     for user_id in unchecked_user_ids:
#         missed_users[user_id] = missed_users.get(user_id, 0) + 1
#     channel_data["missed_users"] = missed_users
#     print(f"DEBUG: Updated missed_users in cache: {channel_data['missed_users']}")
#
#     # --- Reset daily check-ins for the new day (empty the list) ---
#     channel_data["dailyCheckedUsers"] = []
#     print(f"DEBUG: dailyCheckedUsers reset in cache.")
#
#     # --- Clean up 'users' data if check-in count is zero ---
#     # Corrected line for the SyntaxError:
#     channel_data["users"] = {k: v for k, v in channel_data.get("users", {}).items() if v > 0}
#     print(f"DEBUG: Cleaned 'users' data in cache.")
#
#     # --- Save all updated channel data to the database ---
#     await save_specific_data_to_db(guild_id, channel_id, channel_data)
#     print(f"INFO: Channel {channel_id} data saved to DB after reset.")
#
#     # --- Prepare check-in leaderboard for the embed ---
#     checkins_by_real_name = {}
#     for user_id, checkins in channel_data.get("users", {}).items():
#         try:
#             user = await bot.fetch_user(user_id)
#             real_name = channel_data.get("userToReal", {}).get(str(user_id), user.display_name)
#         except (discord.NotFound, discord.HTTPException):
#             real_name = f"Unknown User ({user_id})"
#         checkins_by_real_name[real_name] = checkins_by_real_name.get(real_name, 0) + checkins
#     sorted_checkins = sorted(checkins_by_real_name.items(), key=lambda x: x[1], reverse=True)
#     leaderboard_message = (
#         "\n".join(
#             f"{i + 1}. **{real_name}**: {checkins} check-in(s)"
#             for i, (real_name, checkins) in enumerate(sorted_checkins)
#         )
#         if sorted_checkins else "No valid check-ins recorded."
#     )
#     print(f"DEBUG: Leaderboard message prepared.")
#
#     # --- Prepare missed check-in leaderboard for the embed ---
#     missed_by_real_name = {}
#     for user_id, missed in channel_data.get("missed_users", {}).items():
#         try:
#             user = await bot.fetch_user(user_id)
#             real_name = channel_data.get("userToReal", {}).get(str(user_id), user.display_name)
#         except (discord.NotFound, discord.HTTPException):
#             real_name = f"Unknown User ({user_id})"
#         missed_by_real_name[real_name] = missed_by_real_name.get(real_name, 0) + missed
#     sorted_missed = sorted(missed_by_real_name.items(), key=lambda x: x[1], reverse=True)
#     missed_leaderboard = (
#         "\n".join(
#             f"{i + 1}. **{real_name}**: {missed} missed check-in(s)"
#             for i, (real_name, missed) in enumerate(sorted_missed)
#         )
#         if sorted_missed else "No missed check-ins."
#     )
#     print(f"DEBUG: Missed leaderboard message prepared.")
#
#     # --- Send the comprehensive summary embed to the channel ---
#     if channel:
#         embed = discord.Embed(
#             title=f"Daily Check-in Summary — {formatted_date}",
#             description=f"Here is the breakdown of today's check-ins for this channel (<#{channel_id}>):",
#             color=discord.Color.blue()
#         )
#         # Add the AI-generated summary as the first field
#         embed.add_field(name="AI-Generated Daily Summary (since last reset)", value=gemini_summary_text, inline=False)
#
#         # Add the lists of checked-in and unchecked users
#         embed.add_field(name="Checked-in Users", value=checked_users_list_str, inline=False)
#         embed.add_field(name="Users Who Didn't Check-in", value=unchecked_list_str, inline=False)
#
#         # Add the leaderboards
#         embed.add_field(name="Check-in Leaderboard", value=leaderboard_message, inline=False)
#         embed.add_field(name="Missed Check-ins Leaderboard", value=missed_leaderboard, inline=False)
#
#         # Add the reset notice
#         embed.add_field(name="Reset Notice", value="Daily check-in data has been reset for this channel.", inline=False)
#
#         try:
#             await channel.send(embed=embed)
#             print(f"INFO: Sent reset summary to channel {channel_id} in guild {guild_id}.")
#         except discord.errors.Forbidden:
#             print(
#                 f"ERROR: Missing permissions to send message in channel {channel_id} of guild {guild_id}. Check bot role permissions.")
#         except discord.errors.HTTPException as e:
#             print(f"ERROR: Error sending message to channel {channel_id} of guild {guild_id}: {e}")
#         except Exception as e:
#             print(
#                 f"CRITICAL ERROR: Unexpected error while sending reset summary to channel {channel_id} of guild {guild_id}: {e}")
#     else:
#         print(f"WARNING: Channel {channel_id} not found or inaccessible in guild {guild_id}. Summary not sent.")
async def _perform_channel_reset(guild_id, channel_id, channel_data, now_guild_tz, formatted_date):
    """
    Performs the reset for a channel: calculates and posts leaderboards, resets daily check-in lists,
    and persists changes to Postgres. The Gemini summary feature has been removed.
    """
    print(
        f"INFO: Starting reset process for channel {channel_id} in guild {guild_id} at {now_guild_tz.strftime('%Y-%m-%d %H:%M:%S')}.")

    # --- Start time for summary fetch (kept for message fetching, but no longer used for Gemini) ---
    start_time_for_summary_fetch_utc = channel_data.get("last_reset_time")
    if not start_time_for_summary_fetch_utc or not isinstance(start_time_for_summary_fetch_utc,
                                                              datetime) or not start_time_for_summary_fetch_utc.tzinfo:
        guild_settings = await get_guild_settings(guild_id)
        timezone_str = guild_settings.get("timezone", "America/Los_Angeles")
        try:
            guild_tz = pytz.timezone(timezone_str)
        except pytz.exceptions.UnknownTimeZoneError:
            guild_tz = pytz.timezone("America/Los_Angeles")

        start_of_day_guild_tz = now_guild_tz.replace(hour=0, minute=0, second=0, microsecond=0)
        start_time_for_summary_fetch_utc = start_of_day_guild_tz.astimezone(pytz.utc)
        print(f"DEBUG: Using fallback start time for summary fetch: {start_time_for_summary_fetch_utc}")
    else:
        print(f"DEBUG: Using last_reset_time for summary fetch: {start_time_for_summary_fetch_utc}")

    # --- The logic below for fetching messages is kept but modified to only gather checked users,
    #     as it was previously coupled with the Gemini summary logic.
    #     However, since the core reset logic relies on channel_data["dailyCheckedUsers"],
    #     which is handled by other parts of the bot on check-in, the message fetching
    #     loop below is largely redundant for the *reset* process itself
    #     and can be safely removed, as the lists are built later from channel_data.
    #     The original code's message fetching was ONLY for the Gemini summary.

    # Find the channel object
    channel = bot.get_channel(channel_id)
    if not channel:
        print(
            f"WARNING: Channel {channel_id} not found or inaccessible in guild {guild_id}. Cannot send summary.")

    # --- NO GEMINI/AI SUMMARY LOGIC HERE ---

    # --- Save the current reset time to the channel data ---
    channel_data["last_reset_time"] = now_guild_tz.astimezone(pytz.utc)

    # --- Prepare list of users who checked in for the day that just ended ---
    checked_users_names = []
    for user_id in channel_data.get("dailyCheckedUsers", []):
        try:
            user = await bot.fetch_user(user_id)
            real_name = channel_data.get("userToReal", {}).get(str(user_id), user.display_name)
            checked_users_names.append(real_name)
        except (discord.NotFound, discord.HTTPException):
            checked_users_names.append(f"Unknown User ({user_id})")
    checked_users_list_str = "\n".join(checked_users_names) if checked_users_names else "No users checked in today."
    print(f"DEBUG: Checked users for summary: {checked_users_names}")

    # --- Find users who missed check-in for the day that just ended ---
    unchecked_user_ids = []
    unchecked_users_names = []
    guild = bot.get_guild(guild_id)
    if guild:
        for member in guild.members:
            if not member.bot and member.id not in channel_data.get("banned_users",
                                                                    set()) and member.id not in channel_data.get(
                "dailyCheckedUsers", []):
                try:
                    real_name = channel_data.get("userToReal", {}).get(str(member.id), member.display_name)
                except (discord.NotFound, discord.HTTPException):
                    real_name = f"Unknown User ({member.id})"
                unchecked_users_names.append(real_name)
                unchecked_user_ids.append(member.id)
    unchecked_list_str = "\n".join(unchecked_users_names) if unchecked_users_names else "Everyone checked in today!"
    print(f"DEBUG: Unchecked users for summary: {unchecked_users_names}")

    # --- Update missed_users dict (for persistent tracking of consecutive misses) ---
    missed_users = channel_data.get("missed_users", {})
    for user_id in unchecked_user_ids:
        missed_users[user_id] = missed_users.get(user_id, 0) + 1
    channel_data["missed_users"] = missed_users
    print(f"DEBUG: Updated missed_users in cache: {channel_data['missed_users']}")

    # --- Reset daily check-ins for the new day (empty the list) ---
    channel_data["dailyCheckedUsers"] = []
    print(f"DEBUG: dailyCheckedUsers reset in cache.")

    # --- Clean up 'users' data if check-in count is zero ---
    # Corrected line for the SyntaxError:
    channel_data["users"] = {k: v for k, v in channel_data.get("users", {}).items() if v > 0}
    print(f"DEBUG: Cleaned 'users' data in cache.")

    # --- Save all updated channel data to the database ---
    await save_specific_data_to_db(guild_id, channel_id, channel_data)
    print(f"INFO: Channel {channel_id} data saved to DB after reset.")

    # --- Prepare check-in leaderboard for the embed ---
    checkins_by_real_name = {}
    for user_id, checkins in channel_data.get("users", {}).items():
        try:
            user = await bot.fetch_user(user_id)
            real_name = channel_data.get("userToReal", {}).get(str(user_id), user.display_name)
        except (discord.NotFound, discord.HTTPException):
            real_name = f"Unknown User ({user_id})"
        checkins_by_real_name[real_name] = checkins_by_real_name.get(real_name, 0) + checkins
    sorted_checkins = sorted(checkins_by_real_name.items(), key=lambda x: x[1], reverse=True)
    leaderboard_message = (
        "\n".join(
            f"{i + 1}. **{real_name}**: {checkins} check-in(s)"
            for i, (real_name, checkins) in enumerate(sorted_checkins)
        )
        if sorted_checkins else "No valid check-ins recorded."
    )
    print(f"DEBUG: Leaderboard message prepared.")

    # --- Prepare missed check-in leaderboard for the embed ---
    missed_by_real_name = {}
    for user_id, missed in channel_data.get("missed_users", {}).items():
        try:
            user = await bot.fetch_user(user_id)
            real_name = channel_data.get("userToReal", {}).get(str(user_id), user.display_name)
        except (discord.NotFound, discord.HTTPException):
            real_name = f"Unknown User ({user_id})"
        missed_by_real_name[real_name] = missed_by_real_name.get(real_name, 0) + missed
    sorted_missed = sorted(missed_by_real_name.items(), key=lambda x: x[1], reverse=True)
    missed_leaderboard = (
        "\n".join(
            f"{i + 1}. **{real_name}**: {missed} missed check-in(s)"
            for i, (real_name, missed) in enumerate(sorted_missed)
        )
        if sorted_missed else "No missed check-ins."
    )
    print(f"DEBUG: Missed leaderboard message prepared.")

    # --- Send the comprehensive summary embed to the channel ---
    if channel:
        embed = discord.Embed(
            title=f"Daily Check-in Summary — {formatted_date}",
            description=f"Here is the breakdown of today's check-ins for this channel (<#{channel_id}>):",
            color=discord.Color.blue()
        )
        # The AI-generated summary field has been removed.

        # Add the lists of checked-in and unchecked users
        embed.add_field(name="Checked-in Users", value=checked_users_list_str, inline=False)
        embed.add_field(name="Users Who Didn't Check-in", value=unchecked_list_str, inline=False)

        # Add the leaderboards
        embed.add_field(name="Check-in Leaderboard", value=leaderboard_message, inline=False)
        embed.add_field(name="Missed Check-ins Leaderboard", value=missed_leaderboard, inline=False)

        # Add the reset notice
        embed.add_field(name="Reset Notice", value="Daily check-in data has been reset for this channel.", inline=False)

        try:
            await channel.send(embed=embed)
            print(f"INFO: Sent reset summary to channel {channel_id} in guild {guild_id}.")

            # --- NEW: Send overflow text in additional messages if needed ---
            MAX_DISCORD_MSG_LEN = 2000

            def split_if_needed(label, text):
                """Splits long text into chunks under Discord's 2000-char limit."""
                if len(text) <= MAX_DISCORD_MSG_LEN:
                    return [f"**{label}:**\n{text}"]
                # break into safe chunks
                chunks = []
                current = f"**{label}:**\n"
                for line in text.split("\n"):
                    if len(current) + len(line) + 1 <= MAX_DISCORD_MSG_LEN:
                        current += line + "\n"
                    else:
                        chunks.append(current)
                        current = line + "\n"
                if current:
                    chunks.append(current)
                return chunks

            # Checked-in overflow
            checked_chunks = split_if_needed("Checked-in Users (continued)", checked_users_list_str)

            # Unchecked overflow
            unchecked_chunks = split_if_needed("Users Who Didn't Check-in (continued)", unchecked_list_str)

            # Leaderboard overflow
            leaderboard_chunks = split_if_needed("Check-in Leaderboard (continued)", leaderboard_message)

            # Missed leaderboard overflow
            missed_chunks = split_if_needed("Missed Check-ins Leaderboard (continued)", missed_leaderboard)

            # Send only the *overflow* (if any)
            for chunk_list in [checked_chunks, unchecked_chunks, leaderboard_chunks, missed_chunks]:
                if len(chunk_list) > 1:  # means it overflowed
                    for chunk in chunk_list[1:]:
                        try:
                            await channel.send(chunk)
                        except Exception as e:
                            print(f"ERROR: Failed sending overflow chunk: {e}")

        except discord.errors.Forbidden:
            print(
                f"ERROR: Missing permissions to send message in channel {channel_id} of guild {guild_id}. Check bot role permissions.")
        except discord.errors.HTTPException as e:
            print(f"ERROR: Error sending message to channel {channel_id} of guild {guild_id}: {e}")
        except Exception as e:
            print(
                f"CRITICAL ERROR: Unexpected error while sending reset summary to channel {channel_id} of guild {guild_id}: {e}")
    else:
        print(f"WARNING: Channel {channel_id} not found or inaccessible in guild {guild_id}. Summary not sent.")

@bot.event
async def on_ready():
   print(f"INFO: Logged in as {bot.user}")
   initialize_database()
   await load_all_data_from_db()  # Load all existing data into cache on startup


   for guild in bot.guilds:
       await set_admin_for_guild(guild)  # Ensure admins are set for each guild


   if not resetTime.is_running():
       resetTime.start()
       print("INFO: resetTime task started.")
   else:
       print("INFO: resetTime task already running.")
   print("INFO: Bot is ready and running!")




bot.run(BOT_TOKEN)

