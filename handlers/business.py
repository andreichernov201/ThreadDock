from aiogram import Router
from aiogram.types import BusinessConnection

from services.application import Application

router = Router()


@router.business_connection()
async def connection_changed(connection: BusinessConnection, app: Application) -> None:
    app.connection_changed(connection)
