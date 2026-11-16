from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from core import texts as T

router = Router()


@router.message(CommandStart())
async def handle_start(message: Message) -> None:
    await message.answer(T.HANDLERS_START_WELCOME)
