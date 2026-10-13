/* Mini App: выбор или создание альбома Google Photos. */
(function () {
    "use strict";

    const tg = window.Telegram ? window.Telegram.WebApp : null;
    const params = new URLSearchParams(window.location.search);
    const sessionId = params.get("session_id") || "";

    const el = {
        subtitle: document.getElementById("subtitle"),
        createCard: document.getElementById("create-card"),
        listCard: document.getElementById("list-card"),
        newAlbum: document.getElementById("new-album"),
        createBtn: document.getElementById("create-btn"),
        search: document.getElementById("search"),
        albums: document.getElementById("albums"),
        listHint: document.getElementById("list-hint"),
        error: document.getElementById("error"),
        overlay: document.getElementById("overlay"),
        overlayText: document.getElementById("overlay-text"),
    };

    let albums = [];
    let submitting = false;

    if (tg) {
        tg.ready();
        tg.expand();
    }

    function showError(text) {
        el.error.textContent = text;
        el.error.hidden = false;
    }

    function clearError() {
        el.error.hidden = true;
        el.error.textContent = "";
    }

    function setOverlay(visible, text) {
        if (text) {
            el.overlayText.textContent = text;
        }
        el.overlay.hidden = !visible;
    }

    function initData() {
        return tg && tg.initData ? tg.initData : "";
    }

    async function api(path, options) {
        const config = Object.assign({ headers: {} }, options || {});
        config.headers = Object.assign(
            {
                "X-Telegram-Init-Data": initData(),
                "Content-Type": "application/json",
            },
            config.headers
        );

        const response = await fetch(path, config);
        let payload = null;
        try {
            payload = await response.json();
        } catch (err) {
            payload = null;
        }

        if (!response.ok) {
            const message =
                (payload && payload.error) ||
                "[HTTP " + response.status + "] Запрос не выполнен.";
            throw new Error(message);
        }
        return payload;
    }

    function renderSkeleton() {
        el.albums.innerHTML = '<div class="skeleton"></div>'.repeat(3);
    }

    function renderAlbums() {
        const query = el.search.value.trim().toLowerCase();
        const filtered = query
            ? albums.filter((album) => album.title.toLowerCase().includes(query))
            : albums;

        el.albums.innerHTML = "";

        if (!filtered.length) {
            el.listHint.textContent = albums.length
                ? "Ничего не найдено по запросу."
                : "Доступных альбомов нет — создайте новый выше.";
            return;
        }

        el.listHint.textContent = "";
        const fragment = document.createDocumentFragment();

        filtered.forEach((album) => {
            const button = document.createElement("button");
            button.type = "button";
            button.className = "album";
            button.setAttribute("role", "listitem");

            const icon = document.createElement("span");
            icon.className = "album-icon";
            icon.textContent = "🗂";

            const body = document.createElement("span");
            body.className = "album-body";

            const title = document.createElement("span");
            title.className = "album-title";
            title.textContent = album.title;

            const meta = document.createElement("span");
            meta.className = "album-meta";
            meta.textContent = album.mediaItemsCount + " фото";

            body.appendChild(title);
            body.appendChild(meta);
            button.appendChild(icon);
            button.appendChild(body);

            button.addEventListener("click", function () {
                submit({ album_id: album.id }, "Загружаю в «" + album.title + "»…");
            });

            fragment.appendChild(button);
        });

        el.albums.appendChild(fragment);
    }

    async function submit(payload, overlayText) {
        if (submitting) {
            return;
        }
        if (!sessionId) {
            showError("[HTTP 400] Bad Request: В ссылке отсутствует session_id.");
            return;
        }

        submitting = true;
        clearError();
        setOverlay(true, overlayText || "Отправляю…");

        try {
            await api("/api/select", {
                method: "POST",
                body: JSON.stringify({
                    session_id: sessionId,
                    album_id: payload.album_id || null,
                    new_album_title: payload.new_album_title || null,
                }),
            });

            if (tg) {
                tg.close();
            } else {
                setOverlay(false);
                el.subtitle.textContent = "Готово, можно закрыть страницу.";
            }
        } catch (err) {
            submitting = false;
            setOverlay(false);
            showError(err.message);
            if (tg && tg.showAlert) {
                tg.showAlert(err.message);
            }
        }
    }

    async function load() {
        if (!sessionId) {
            el.subtitle.textContent = "Сессия не найдена.";
            showError("[HTTP 400] Bad Request: В ссылке отсутствует session_id.");
            el.createCard.hidden = true;
            el.listCard.hidden = true;
            return;
        }

        renderSkeleton();

        try {
            const data = await api("/api/albums?session_id=" + encodeURIComponent(sessionId));
            albums = Array.isArray(data.albums) ? data.albums : [];
            el.subtitle.textContent =
                "Фотографий к загрузке: " + (data.photo_count || 0);

            if (data.albums_error) {
                showError(
                    data.albums_error +
                        "\nСписок альбомов недоступен, но новый альбом создать можно."
                );
            }
            renderAlbums();
        } catch (err) {
            el.albums.innerHTML = "";
            el.subtitle.textContent = "Не удалось загрузить данные.";
            el.listHint.textContent = "";
            showError(err.message);
        }
    }

    el.newAlbum.addEventListener("input", function () {
        el.createBtn.disabled = el.newAlbum.value.trim().length === 0;
    });

    el.newAlbum.addEventListener("keydown", function (event) {
        if (event.key === "Enter" && el.newAlbum.value.trim()) {
            submit({ new_album_title: el.newAlbum.value.trim() }, "Создаю альбом…");
        }
    });

    el.createBtn.addEventListener("click", function () {
        const title = el.newAlbum.value.trim();
        if (title) {
            submit({ new_album_title: title }, "Создаю альбом…");
        }
    });

    el.search.addEventListener("input", renderAlbums);

    load();
})();
