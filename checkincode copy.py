import json
import asyncio
from discord import message
import interactions
# from interactions.models.internal.context import SlashContext
# from interactions import SlashContext, OptionType, slash_option
import discord
import random
import os
import re
# from interactions import Client, Intents
# from interactions.ext import prefixed_commands
# from interactions.ext.prefixed_commands import prefixed_command, PrefixedContext
from discord.ext import commands
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, timedelta
import pytz
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

MONGO_URI = os.getenv("MONGO_URI")  # Store this in your .env file
mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client["checkinbot"]  # Database name
users_collection = db["users"]

load_dotenv()


BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")


bot = commands.Bot(command_prefix=commands.when_mentioned_or("c.", "C."),intents=discord.Intents.all())
channel_data = {}

async def get_channel_data(guild):
    if isinstance(guild, discord.Guild):
        guild_id = guild.id
    elif isinstance(guild, int):  # If it's already an ID (int)
        guild_id = guild
        guild = await bot.fetch_guild(guild_id)  # Fetch the actual Guild object from its ID
    else:
        raise ValueError("Expected guild to be a discord.Guild object or an integer.")

        # Fetch the existing data from the database
    data = await db.channels.find_one({'_id': guild_id})

    # If no data is found, initialize the data
    if data is None:
        data = {
            '_id': guild_id,
            'users': {},
            'dailyCheckedUsers': [],
            'allUsers': {},
            'userToReal': {},
            'realPeople': {},
            'banned_users': set(),
            'require_media': False,
            'newReset': '235959',
            'timezone': 'America/Los_Angeles',
            'server_admins': [],
            'word_min': 1,
            'missed_users': {}
        }

        # Convert sets to lists before inserting
        data['banned_users'] = list(data['banned_users'])
        data['server_admins'] = list(data['server_admins'])

        # Insert the data into the database
        await db.channels.insert_one(data)
    else:
        # If data exists, check if 'server_admins' is None
        if "server_admins" not in data or data["server_admins"] is None:
            data["server_admins"] = []  # Initialize as an empty list

        # Ensure the guild has an owner before accessing guild.owner.id
        if guild.owner is not None:
            data["server_admins"].append(guild.owner.id)
        else:
            # Handle the case when there's no owner
            print(f"Warning: Guild {guild_id} has no owner.")
            # Optionally, handle it, for example, by skipping adding the owner to the admin list.

        # Update the document in the database
        await db.channels.update_one({'_id': guild_id}, {'$set': {'server_admins': data["server_admins"]}})

@bot.command()
async def m(ctx):
    await ctx.send("**Commands accessible by everyone**:"
                   "\nc.m - Manual"
                   "\nc.c - Post check-in"
                   "\nc.t - Check who sent a check-in today"
                   "\nc.wl - Leaderboard/Streak for checking in"
                   "\nc.ll - Leaderboard/Streak for NOT checking in"
                   "\n**Commands only accessible by the server owner**:"
                   "\nc.n - Tracks certain users/changes usernames to their real names. Just type 'c.n' to track everyone. This command is highly recommended to use"
                   "\nc.a - Adds a certain number of check-ins to a user's check-in count"
                   "\nc.r - Sets the reset time for check-ins. The default reset time is set to 11:59:59 PM in whatever timezone the first admin (the one who invited the bot) is in."
                   "\nc.e - Requires evidence for check-ins (just type 'c.e' to toggle)"
                   "\nc.d - Removes access for user(s) to check in"
                   "\nc.g - Gives/transfers admin commands to another user (enter their username)"
                   "\nc.tz - Lists all timezones available"
                   "\nc.w - Sets a minimum number of words required in the check-in"
                   "\nc.lr - Reset the leaderboard, type in 'wl' or 'll' to choose which leaderboard to reset")




now_utc = datetime.now(pytz.utc)
now_pst = now_utc.astimezone(pytz.timezone('America/Los_Angeles'))

# @bot.event
# async def on_guild_join(guild):
#     data = get_channel_data(guild.id)
#     if data["server_admins"]:
#         return
#     inviter = None
#     async for entry in guild.audit_logs(action=discord.AuditLogAction.bot_add):
#         if entry.target.id == bot.user.id:
#             inviter = entry.user
#             break
#     if inviter:
#         data["server_admins"].add(inviter.id)
#     else:
#         data["server_admins"].add(guild.owner_id)
#     print(f"Bot added to {guild.name}. Admin: {data['server_admins']}")

async def set_admin_for_guild(guild):
    """Sets the server admin as either the inviter or the owner if not already set."""
    data = await get_channel_data(guild.id)

    # Check if data is None, meaning no data found for this guild
    if data is None:
        print(f"⚠️ No data found for guild {guild.id}. Initializing data...")
        data = {
            '_id': guild.id,
            'server_admins': [],
            'users': {},
            'dailyCheckedUsers': [],
            'allUsers': {},
            'userToReal': {},
            'realPeople': {},
            'banned_users': [],
            'require_media': False,
            'newReset': '235959',
            'timezone': 'America/Los_Angeles',
            'word_min': 1,
            'missed_users': {}
        }
        # Insert the new data if it doesn't exist
        await db.channels.update_one(
            {"_id": guild.id},
            {"$setOnInsert": data},
            upsert=True  # This will insert the document only if it doesn't exist
        )

    # Now we can safely check if 'server_admins' exists in data
    if "server_admins" not in data or data["server_admins"] is None:
        data["server_admins"] = []

    if data["server_admins"]:
        print("Server admins exist:", data["server_admins"])

    inviter = None
    try:
        async for entry in guild.audit_logs(action=discord.AuditLogAction.bot_add, limit=1):
            if entry.target.id == bot.user.id:
                inviter = entry.user
                break
    except discord.Forbidden:
        print(f"⚠️ Missing permissions to access audit logs in {guild.name}")

    if inviter:
        data["server_admins"].append(inviter.id)
        print(f"✅ Admin for {guild.name} set to inviter: {inviter.id}")
    else:
        data["server_admins"].append(guild.owner_id)
        print(f"✅ Admin for {guild.name} set to owner: {guild.owner_id}")

    print(f"🔹 Current admins for {guild.name}: {data['server_admins']}")
    # Update the database with the new server admins list
    await db.channels.update_one(
        {"_id": guild.id},
        {"$set": {"server_admins": data["server_admins"]}},
        upsert=True  # Ensure we don't get a duplicate key error
    )
@bot.event
async def on_guild_join(guild):
    """When the bot joins a new server, assign an admin."""
    await set_admin_for_guild(guild)

def is_admin(ctx):
   data = get_channel_data(ctx.guild.id)
   return ctx.author.id in data["server_admins"]


# @bot.command()
# async def g(ctx, action: str, member: discord.Member = None):
#    if ctx.author.id != ctx.guild.owner_id:
#        await ctx.send(f"{ctx.author.name}, only the server owner can manage admins.")
#        return
#
#
#    guild_id = ctx.guild.id
#    if guild_id not in server_admins:
#        server_admins[guild_id] = set()
#
#
#    if action.lower() == "add" and member:
#        server_admins[guild_id].add(member.id)
#        await ctx.send(f"{member.mention} has been added as a server admin.")
#    elif action.lower() == "remove" and member:
#        if member.id in server_admins[guild_id]:
#            server_admins[guild_id].remove(member.id)
#            await ctx.send(f"{member.mention} has been removed as a server admin.")
#        else:
#            await ctx.send(f"{member.mention} is not a server admin.")
#    elif action.lower() == "list":
#        admin_list = [f"<@{admin_id}>" for admin_id in server_admins.get(guild_id, [])]
#        admins = ", ".join(admin_list) if admin_list else "No admins assigned."
#        await ctx.send(f"**Server Admins:** {admins}")
#    else:
#        await ctx.send("Invalid command. Use `c.g add @User`, `c.g remove @User`, or `c.g list`.")
@bot.command()
async def g(ctx, action: str, member: discord.Member = None):
    data = get_channel_data(ctx.guild.id)
    guild_id = ctx.guild.id
    if not data["server_admins"]:
        data["server_admins"].add(ctx.guild.owner_id)


    if ctx.author.id not in data["server_admins"]:
        await ctx.send(f"{ctx.author.name}, only a server admin can manage admins.")
        return

    if action.lower() == "add" and member:
        data["server_admins"].add(member.id)
        await ctx.send(f"{member.mention} has been added as a server admin.")
    elif action.lower() == "remove" and member:
        if member.id in data["server_admins"] and member.id != ctx.guild.owner_id:
            data["server_admins"].remove(member.id)
            await ctx.send(f"{member.mention} has been removed as a server admin.")
        else:
            await ctx.send(f"{member.mention} is not a server admin or cannot be removed.")
    elif action.lower() == "list":
        admin_list = [f"<@{admin_id}>" for admin_id in data["server_admins"]]
        admins = ", ".join(admin_list) if admin_list else "No admins assigned."
        await ctx.send(f"**Server Admins:** {admins}")
    else:
        await ctx.send("Invalid command. Use `c.g add @User`, `c.g remove @User`, or `c.g list`.")
# @bot.command()
# async def c(ctx, *checkIn):
#   user_id = ctx.author.id
#   message = ctx.message
#   user_name = ctx.author.name
#   text = ' '.join(checkIn)
#   has_link = "https://" in text or "http://" in text
#   has_image = any(
#       attachment.filename.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp', '.HEIC', '.pdf')) for attachment in message.attachments
#   )
#   if not checkIn and not has_image:
#     await ctx.send(f"{ctx.author.name}, please enter a check-in.")
#   elif has_link or has_image:
#     if user_id in users:
#       users[user_id] += 1
#     else:
#       users[user_id] = 1
#     if user_name not in dailyCheckedUsers:
#       dailyCheckedUsers.append(user_name)
#       await ctx.send(f"{ctx.author.mention}, you've successfully checked in today!")
#     else:
#       await ctx.send(f"{ctx.author.name}, you've already checked in today!")
#       users[user_id] -= 1
#   else:
#     await ctx.send(f"{ctx.author.name}, please include an image or a link.")
@bot.command()
async def c(ctx, *checkIn):
    data = get_channel_data(ctx.channel.id)
    user_id = ctx.author.id
    user_name = ctx.author.name
    text = ' '.join(checkIn)
    word_count = len(text.split())
    has_link = "https://" in text or "http://" in text
    has_image = any(
        attachment.filename.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp', '.HEIC', '.pdf')) for attachment in ctx.message.attachments
    )
    if not checkIn and not has_image and data["require_media"]:
      await ctx.send(f"{ctx.author.name}, please enter a check-in with an image or link.")
      return
    if user_name in data["banned_users"]:
      await ctx.send(f"{ctx.author.name}, you are currently banned from checking in.")
      return
    if data["require_media"] and not (has_link or has_image):
      await ctx.send(f"{ctx.author.name}, please include an image or link.")
      return
    if text == "":
      await ctx.send(f"{ctx.author.name}, please include a check-in.")
      return
    if word_count < data["word_min"]:
      await ctx.send(f"{ctx.author.name}, please make sure your check-in has at least {data['word_min']} word(s).")
      return
    if user_id in data["users"]:
      data["users"][user_id] += 1
    else:
      data["users"][user_id] = 1
    if user_name not in data["dailyCheckedUsers"]:
      data["dailyCheckedUsers"].append(user_name)
      await db.channels.update_one({"_id": ctx.channel.id}, {"$set": data})
      await ctx.send(f"{ctx.author.mention}, you've successfully checked in today!")
    else:
      await ctx.send(f"{ctx.author.name}, you've already checked in today!")
      data["users"][user_id] -= 1




@bot.command()
async def e(ctx):
    data = get_channel_data(ctx.channel.id)
    if not is_admin(ctx):
      await ctx.send(f"{ctx.author.name}, this command is only accessible to admins.")
      return
    data["require_media"] = not data["require_media"]
    status = "no longer" if not data["require_media"] else "now"
    await ctx.send(f"Check-ins **{status}** require evidence (an image or link).")




@bot.command()
async def wl(ctx):
    data = get_channel_data(ctx.channel.id)
    sorted_users = sorted(data["users"].items(), key=lambda x: x[1], reverse=True)
    leaderboard_message = "**Leaderboard:**\n"
    for rank, (user_id, checkins) in enumerate(sorted_users, 1):
      # realNames = [userToReal.get(user,user) for user in dailyCheckedUsers]
      # leaderboardList = "\n".join(realNames)
      username = bot.get_user(user_id).name if bot.get_user(user_id) else str(user_id)
      real_name = data["realPeople"].get(username, username)
      leaderboard_message += f"{rank}. {real_name}: {checkins} check-in(s)\n"
    await ctx.send(leaderboard_message)

@bot.command()
async def ll(ctx):
    data = get_channel_data(ctx.channel.id)
    missed_users = data["missed_users"]
    sorted_missed = sorted(missed_users.items(), key=lambda x: x[1], reverse=True)
    missed_leaderboard = "**Missed Check-ins Leaderboard:**\n"
    for rank, (user_id, missed) in enumerate(sorted_missed, 1):
        user = bot.get_user(user_id)
        username = user.name if user else str(user_id)
        real_name = data["realPeople"].get(username, username)
        missed_leaderboard += f"{rank}. {real_name}: {missed} missed check-in(s)\n"
    await ctx.send(missed_leaderboard)



@bot.command()
async def t(ctx):
    data = get_channel_data(ctx.channel.id)
    if data["dailyCheckedUsers"]:
      realNamesList = [data["userToReal"].get(user, user) for user in data["dailyCheckedUsers"]]
      userList = "\n".join(realNamesList)
      unchecked_users = [data["userToReal"].get(user, user) for user in data["userToReal"].keys() if user not in data["dailyCheckedUsers"] and user not in data["banned_users"]]
      unchecked_list = "\n".join(unchecked_users) if unchecked_users else "Everyone checked in today!"
      await ctx.send(f"**Users who checked in today:**\n{userList}"
                     f"\n\n**Users who have yet to check-in today:**\n{unchecked_list}")
    else:
      await ctx.send("No users have checked in today yet.")






# @bot.command()
# async def u(ctx, *, usernames):
#   global allUsers
#   names = [name.strip() for name in usernames.split(",")]
#   for name in names:
#     allUsers[name] = 0
#   userKeys = "\n".join(allUsers.keys())
#   await ctx.send(f"Tracking the following users for check-ins:\n{userKeys}")




@bot.command()
async def n(ctx, *, realNames=None):
    data = get_channel_data(ctx.channel.id)
    if not is_admin(ctx):
      await ctx.send(f"{ctx.author.name}, this command is only accessible to admins.")
      return
    if realNames:
      pairs = [pair.strip() for pair in realNames.split(",")]
      for pair in pairs:
        try:
          username, real_name = pair.split(":")
          username = username.strip()
          real_name = real_name.strip()
          data["userToReal"][username] = real_name
          data["realPeople"][username] = real_name
        except ValueError:
          await ctx.send(f"Invalid format for '{pair}'. Use 'username: realname' format.")
          return
    else:
      data["userToReal"] = {}
      data["realPeople"] = {}
      for member in ctx.guild.members:
        if not member.bot and member.name not in data["banned_users"]:
          data["userToReal"][member.name] = member.display_name
          data["realPeople"][member.name] = member.display_name




    # user_mappings = "\n".join([f"{user} -> {real}" for user, real in userToReal.items()])
    # realPeople["user_mappings"] = ", ".join([f"{user}:{real}" for user, real in userToReal.items()])
    user_mappings = "\n".join([f"{user} -> {real}" for user, real in data["realPeople"].items()])
    await ctx.send(f"Usernames mapped to real names:\n{user_mappings}")




@bot.command()
async def a(ctx, username: str, count: int):
    data = get_channel_data(ctx.channel.id)
    if not is_admin(ctx):
      await ctx.send(f"{ctx.author.name}, this command is only accessible to admins.")
      return
    member = discord.utils.get(ctx.guild.members, name=username)
    if not member:
      await ctx.send(f"User '{username}' not found in the server.")
      return
    user_id = member.id
    user_name = member.name
    data["users"][user_id] = data["users"].get(user_id, 0) + count
    if count >= 0:
        await ctx.send(f"**{count}** check-in(s) have been added to **{user_name}**.")
    else:
        await ctx.send(f"**{abs(count)}** check-in(s) have been removed from **{user_name}**.")

@bot.command()
async def w(ctx, min_lim: int):
    data = get_channel_data(ctx.channel.id)
    if not is_admin(ctx):
        await ctx.send(f"{ctx.author.name}, this command is only accessible to admins.")
        return
    if min_lim <= 0:
        await ctx.send(f"{ctx.author.name}, please enter a positive number (greater than zero).")
    try:
        data["word_min"] = min_lim
        await ctx.send(f"Word minimum set to **{data['word_min']}** words.")
    except ValueError:
        await ctx.send(f"{ctx.author.name}, please enter a number.")



@bot.command()
async def d(ctx, *args):
    data = get_channel_data(ctx.channel.id)
    if not is_admin(ctx):
      await ctx.send(f"{ctx.author.name}, this command is only accessible to admins.")
      return
    if not args:
      await ctx.send(f"Usage: `c.d username1 username2 ...` to ban users, `c.d -u username1 username2 ...` to unban, or `c.d -list` to view banned users.")
      return
    if args[0] == "-list":
      if data["banned_users"]:
        banned_list = ', '.join(data["banned_users"])
        await ctx.send(f"**Banned users**:\n{banned_list}")
      else:
        await ctx.send(f"No users are currently banned from checking in.")
      return
    if args[0] == "-u":
      to_unban = set(args[1:])
      if not to_unban:
        await ctx.send(f"Please specify a username to unban.")
        return
      unbanned = to_unban.intersection(data["banned_users"])
      if unbanned:
        data["banned_users"] -= unbanned
        await ctx.send(f"**Unbanned users**:\n{', '.join(unbanned)}")
      else:
        await ctx.send("No matching users were banned.")
      return
    data["banned_users"].update(args)
    await ctx.send(f"**Banned users**:\n{', '.join(args)}")

@bot.command()
async def tz(ctx, *timezoneset):
    data = get_channel_data(ctx.channel.id)
    if not is_admin(ctx):
        await ctx.send(f"{ctx.author.name}, this command is only accessible to admins.")
        return
    if not timezoneset:
        await ctx.send(f"Usage: `c.tz list` to view all timezones or `c.tz [timezone]` to set one.")
        return
    if timezoneset[0].lower() == "list":
        tz_list = "\n".join(pytz.all_timezones)
        file_path = "/tmp/timezones.txt"
        with open(file_path, "w", encoding="utf-8") as file:
            file.write(tz_list)
        await ctx.send(f"**List of available timezones:**", file=discord.File(file_path))
        return
    if timezoneset:
        tz_string = " ".join(timezoneset)
        if tz_string in pytz.all_timezones:
            data["timezone"] = tz_string
            await ctx.send(f"Timezone set to **{tz_string}**.")
        else:
            await ctx.send(f"Invalid timezone. Use `c.tz list` to see valid timezones.")

@bot.command()
async def lr(ctx, *leader_input):
    data = channel_data.get(ctx.channel.id, {"users": {}})
    data["users"] = {}
    await ctx.send("Leaderboard has been reset.")

@bot.command()
async def r(ctx, *resetTime):
    data = get_channel_data(ctx.channel.id)
    if not is_admin(ctx):
      await ctx.send(f"{ctx.author.name}, this command is only accessible to admins.")
      return
    reset = "".join(resetTime)
    hours = reset[:2].zfill(2)
    minutes = reset[2:4].zfill(2)
    seconds = reset[4:].zfill(2)
    if len(reset) != 6 or not reset.isdigit():
      await ctx.send(f"{ctx.author.name}, please enter a 6-digit number.")
    elif int(hours) > 23 or int(minutes) > 59 or int(seconds) > 59:
      await ctx.send(f"{ctx.author.name}, please keep the time under a 24-hour cycle.")
    else:
      data["newReset"] = reset
      await ctx.send(f"Reset time set to **{hours}:{minutes}:{seconds}**.")


@tasks.loop(seconds=1)
async def resetTime():
    now_utc = datetime.now(pytz.utc)
    for channel_id, data in channel_data.items():
        now_pst = now_utc.astimezone(pytz.timezone(data["timezone"]))
        formatted_date = now_pst.strftime("%m/%d/%Y")
        date_hourminsec = now_pst.strftime('%H%M%S')
        reset_time = data["newReset"]
        if date_hourminsec == reset_time:
            checked_users = "\n".join([data["userToReal"].get(user, user) for user in data["dailyCheckedUsers"]]) if data["dailyCheckedUsers"] else "No users checked in today."
            unchecked_users = [data["userToReal"].get(user, user) for user in data["userToReal"].keys() if user not in data["dailyCheckedUsers"] and user not in data["banned_users"]]
            unchecked_list = "\n".join(unchecked_users) if unchecked_users else "Everyone checked in today!"

            sorted_users = sorted(data["users"].items(), key=lambda x: x[1], reverse=True)
            leaderboard_message = "**Leaderboard:**\n"
            for rank, (user_id, checkins) in enumerate(sorted_users, 1):
                # realNames = [userToReal.get(user,user) for user in dailyCheckedUsers]
                # leaderboardList = "\n".join(realNames)
                username = bot.get_user(user_id).name if bot.get_user(user_id) else str(user_id)
                real_name = data["realPeople"].get(username, username)
                leaderboard_message += f"{rank}. {real_name}: {checkins} check-in(s)\n"

            for user in unchecked_users:
                if user in data["missed_users"]:
                    data["missed_users"][user] += 1
                else:
                    data["missed_users"][user] = 1

            missed_checkins = data["missed_users"]
            sorted_missed = sorted(missed_checkins.items(), key=lambda x: x[1], reverse=True)
            missed_leaderboard = "**Missed Check-ins Leaderboard:**\n"
            for rank, (user_id, missed) in enumerate(sorted_missed, 1):
                username = bot.get_user(user_id).name if bot.get_user(user_id) else str(user_id)
                real_name = data["realPeople"].get(username, username)
                missed_leaderboard += f"{rank}. {real_name}: {missed} missed check-in(s)\n"

            message = (
                f"**Check-ins on {formatted_date}:**\n{checked_users}\n\n"
                f"**Users who DID NOT check in today:**\n{unchecked_list}"
                f"\n\n{leaderboard_message}"
                f"\n\n{missed_leaderboard}"
            )

            # Clear the daily check-ins
            data["dailyCheckedUsers"].clear()

            # Ensure CHANNEL_ID is properly fetched for each channel
            channel = bot.get_channel(int(channel_id))
            if channel:
                await channel.send(f"{message}\nDaily check-in data has been reset.")
            else:
                print(f"Failed to send reset message. Channel {channel_id} not found.")






# if date_hourminsec == reset_time:
#   if dailyCheckedUsers:
#     userList = "\n".join(dailyCheckedUsers)
#     message = f"**Check-ins on {formatted_date}:**\n{userList}\n"
#   else:
#     message = f"No check-ins recorded on **{formatted_date}**.\n"
#   if unchecked_users:
#     unchecked_list = "\n".join(unchecked_users)
#     message = f"**Users who DID NOT check in today:**\n{unchecked_list}"
#   else:
#     message = f"Everyone checked in on **{formatted_date}**.\n"


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    for guild in bot.guilds:
        await on_guild_join(guild)
    resetTime.start()



bot.run(BOT_TOKEN)

