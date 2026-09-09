"""Авторизация до чтения загрузок и ограниченный штатный multipart-парсер."""

from contextlib import closing

from fastapi import HTTPException, Request, params
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from python_multipart.exceptions import MultipartParseError
from starlette.concurrency import run_in_threadpool
from starlette.formparsers import MultiPartException, MultiPartParser, parse_options_header

import db
import images
import users
import voting


# Потолок Caddy 200MB; реальные байты считаются и без Content-Length.
MAX_REQUEST_BYTES = 200_000_000
MAX_FILES = 7  # Пять фото плюс два видео, включая старое singular-поле.
MAX_FIELDS = 128
MAX_FIELD_BYTES = 64 * 1024
MAX_PART_HEADER_BYTES = 16 * 1024
MAX_BOUNDARY_BYTES = 200
MAX_IMPORT_BYTES = 50 * 1024 * 1024


class UploadLimitExceeded(MultiPartException):
    pass


class BoundedMultiPartParser(MultiPartParser):
    """Потоковый разбор Starlette с лимитами до записи очередной части файла."""

    def __init__(self, headers, stream, *, file_limits):
        super().__init__(headers, stream, max_files=MAX_FILES,
                         max_fields=MAX_FIELDS, max_part_size=MAX_FIELD_BYTES)
        self.file_limits = file_limits
        self.complete = False

    def on_part_begin(self):
        super().on_part_begin()
        self.part_bytes = 0
        self.header_bytes = 0

    def _count_header(self, size):
        self.header_bytes += size
        if self.header_bytes > MAX_PART_HEADER_BYTES:
            raise UploadLimitExceeded("Слишком большие заголовки файла")

    def on_header_field(self, data, start, end):
        self._count_header(end - start)
        super().on_header_field(data, start, end)

    def on_header_value(self, data, start, end):
        self._count_header(end - start)
        super().on_header_value(data, start, end)

    def on_headers_finished(self):
        try:
            super().on_headers_finished()
        except MultiPartException as exc:
            if self._current_files > self.max_files or self._current_fields > self.max_fields:
                raise UploadLimitExceeded("Слишком много файлов или полей формы") from exc
            raise

    def on_part_data(self, data, start, end):
        self.part_bytes += end - start
        if self._current_part.file is None:
            limit = MAX_FIELD_BYTES
        else:
            limit = self.file_limits.get(self._current_part.field_name, images.MAX_BYTES)
        if self.part_bytes > limit:
            raise UploadLimitExceeded("Файл или поле формы превышает допустимый размер")
        super().on_part_data(data, start, end)

    def on_end(self):
        self.complete = True
        super().on_end()

    def close_files(self):
        # Включает незавершённые части, ещё не попавшие в FormData.
        for file in self._files_to_close_on_error:
            if not file.closed:
                file.close()

    async def parse(self):
        try:
            _, options = parse_options_header(self.headers.get("content-type", ""))
            if len(options.get(b"boundary", b"")) > MAX_BOUNDARY_BYTES:
                raise UploadLimitExceeded("Слишком длинная граница multipart")
            form = await super().parse()
            # python-multipart.finalize() допускает незавершённую последнюю часть.
            if not self.complete:
                raise MultiPartException("Загрузка формы не завершена")
            return form
        except BaseException:
            # Starlette убирает файлы лишь при некоторых ошибках парсера/IO;
            # разрыв соединения и отмена тоже должны закрыть частичные загрузки.
            self.close_files()
            raise


async def _bounded_stream(request):
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > MAX_REQUEST_BYTES:
            raise UploadLimitExceeded("Запрос превышает допустимый размер")
        # Не держим целиком большой ASGI-фрейм в очереди записи парсера.
        for offset in range(0, len(chunk), MAX_FIELD_BYTES):
            yield chunk[offset:offset + MAX_FIELD_BYTES]


def _authorize_upload(request, *, admin, endpoint_name):
    """Проверка без записей; штатные зависимости повторят авторизацию и CSRF."""
    uid = request.session.get("user_id")
    with closing(db.connect()) as conn:
        user = users.get_user(conn, uid) if uid else None
        if not user or not user["is_active"]:
            if admin:
                request.session.clear()
                raise users.NeedLogin()
            raise HTTPException(401, {"need_login": True,
                                      "msg": "Войди в аккаунт, чтобы продолжить ♥"})

        # Здесь отклоняем лишь явный чужой Origin. Непрозрачный/отсутствующий
        # Origin по-прежнему требует проверки CSRF-токена после разбора формы.
        origin = request.headers.get("origin")
        if origin and origin != "null":
            users.require_same_origin(request)

        if admin:
            import admin_routes

            # current_user также признаёт операторов, недавно добавленных в env.
            # Учитываем эту роль, но ничего не записываем до проверки CSRF.
            if user["telegram_id"] in users.OPERATOR_TG_IDS and user["telegram_id"] != 0:
                user = dict(user)
                user["is_operator"] = 1
            request.state.user = user
            if endpoint_name == "import_json":
                admin_routes._require_operator(request)
            if "cid" in request.path_params:
                admin_routes._cat_or_404(conn, request.path_params["cid"], user)
            if "did" in request.path_params:
                admin_routes._date_or_404(conn, request.path_params["did"], user)
        else:
            import public_routes

            cat = public_routes.active_cat_or_410(conn, request.path_params["token"], request)
            state = voting.get_category_state(conn, cat["id"])
            if not public_routes.proposal_changes_open(state):
                raise HTTPException(409, "Голосование завершено — варианты уже зафиксированы")
            if "date_id" in request.path_params:
                public_routes.own_proposal_or_403(
                    conn, cat, request.path_params["date_id"],
                    public_routes.viewer_token(user),
                )


class UploadRoute(APIRoute):
    """Проверяет запрос до разбора параметров File/Form и зависимостей FastAPI."""

    def get_route_handler(self):
        handler = super().get_route_handler()
        file_fields = {field.alias for field in self.dependant.body_params
                       if isinstance(field.field_info, params.File)}
        if not file_fields:
            return handler
        admin = self.path.startswith("/admin/")
        endpoint_name = self.endpoint.__name__

        async def upload_handler(request: Request):
            content_type, _ = parse_options_header(request.headers.get("content-type", ""))
            if content_type.lower() != b"multipart/form-data":
                return await handler(request)

            await run_in_threadpool(_authorize_upload, request,
                                    admin=admin, endpoint_name=endpoint_name)
            parser = None
            try:
                declared_size = request.headers.get("content-length")
                if declared_size is not None:
                    try:
                        size = int(declared_size)
                    except ValueError:
                        raise MultiPartException("Некорректный размер запроса")
                    if size < 0:
                        raise MultiPartException("Некорректный размер запроса")
                    if size > MAX_REQUEST_BYTES:
                        raise UploadLimitExceeded("Запрос превышает допустимый размер")
                file_limits = {
                    field: (MAX_IMPORT_BYTES if endpoint_name == "import_json" and field == "file"
                            else images.MAX_VIDEO_BYTES if field in {"video", "videos"}
                            else images.MAX_BYTES)
                    for field in file_fields
                }
                parser = BoundedMultiPartParser(request.headers, _bounded_stream(request),
                                               file_limits=file_limits)
                # Request.form() и FastAPI используют этот кэш без второго разбора.
                request._form = await parser.parse()
                return await handler(request)
            except UploadLimitExceeded as exc:
                # Прямой ответ не позволяет HTML-обработчику заменить 413 редиректом.
                return JSONResponse({"detail": exc.message}, status_code=413)
            except (MultiPartException, MultipartParseError):
                return JSONResponse({"detail": "Некорректная или незавершённая форма"}, status_code=400)
            finally:
                if parser is not None:
                    parser.close_files()

        return upload_handler
