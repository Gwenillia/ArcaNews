from . import igdb
from . import search
from . import wishlist

async def setup(bot):
    await search.setup(bot)
    await igdb.setup(bot)
    await wishlist.setup(bot)
