SKILLS = (
    "overall",
    "attack", "defence", "strength", "hitpoints", "ranged", "prayer", "magic",
    "cooking", "woodcutting", "fletching", "fishing", "firemaking", "crafting",
    "smithing", "mining", "herblore", "agility", "thieving", "slayer", "farming",
    "runecrafting", "hunter", "construction",
)
account_filter_code = {
    "NOFILTER": 0,
    "ALL_IRONMAN": 1,
    "HARDCORE_IRONMAN": 2,
    "ULTIMATE_IRONMAN": 3
}
PLAYER_HIGHSCORES_URL = "https://vidyaplace.org/api/highscores/player/{username}"
PLAYER_TRACKER_URL = "https://vidyaplace.org/api/tracker/player/{username}?time={seconds}"
SKILL_TRACKER_URL = "https://vidyaplace.org/api/tracker/skill/{skill}/{page}?time={seconds}&ironman={ironman}"
