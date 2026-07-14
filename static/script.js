// Конфиг выводимых данных в наш псевдо-терминал
const terminalData = {
    help: [
        { text: "$ pechkin --help", isCmd: true },
        { text: "Почтальон Печкин Core Engine v2.0.1", isCmd: false },
        { text: "Доступные слэш-команды:", isCmd: false },
        { text: "  /bcreate <name> [type]   — Создать мост или кросс-сеть", isCmd: false },
        { text: "  /bconnect <name>         — Подключить текущий канал к сети", isCmd: false },
        { text: "  /blist                   — Показать активные мосты сервера", isCmd: false }
    ],
    create: [
        { text: "$ /bcreate name:fandom-news type:single", isCmd: true },
        { text: "[INFO] Подключение к Upstash Redis...", isCmd: false },
        { text: "✅ Мост 'fandom-news' успешно создан!", isCmd: false },
        { text: "Тип: Single (Режим трансляции новостей)", isCmd: false },
        { text: "Скопируйте ID и настройте /bconnect на целевых серверах.", isCmd: false }
    ],
    connect: [
        { text: "$ /bconnect name:fandom-news", isCmd: true },
        { text: "[INFO] Авторизация и проверка прав...", isCmd: false },
        { text: "🔗 Канал #вики-правки успешно присоединен к мосту 'fandom-news'!", isCmd: false },
        { text: "Все поступающие события будут транслироваться сюда.", isCmd: false }
    ],
    list: [
        { text: "$ /blist", isCmd: true },
        { text: "🔍 Сканирование локальных каналов сервера...", isCmd: false },
        { text: "🌐 Активные связи мостов для сервера Wiki-Union:", isCmd: false },
        { text: "📢 Single-Мост (Источник: 🌐 Fandom Server > #logs)", isCmd: false },
        { text: "   ➡️ Трансляция в: <#984321980321> (Локальный)", isCmd: false }
    ]
};

const terminalScreen = document.getElementById("terminal-screen");
const buttons = document.querySelectorAll(".t-btn");

function renderTerminal(cmdKey) {
    terminalScreen.innerHTML = ""; // Очищаем экран
    const lines = terminalData[cmdKey];

    lines.forEach((line, index) => {
        const lineEl = document.createElement("div");
        lineEl.className = "terminal-line";
        lineEl.style.animationDelay = `${index * 150}ms`; // Поочередный вывод строк

        if (line.isCmd) {
            lineEl.innerHTML = `<span class="cmd-prefix">${line.text.slice(0, 2)}</span>${line.text.slice(2)}`;
        } else {
            lineEl.className += " cmd-output";
            lineEl.textContent = line.text;
        }

        terminalScreen.appendChild(lineEl);
    });
}

// Навешиваем клик на кнопки переключения команд
buttons.forEach(btn => {
    btn.addEventListener("click", () => {
        buttons.forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        renderTerminal(btn.dataset.cmd);
    });
});

// Первичный запуск вывода при загрузке страницы
document.addEventListener("DOMContentLoaded", () => {
    renderTerminal("help");
});
