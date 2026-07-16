const liveLine = document.querySelector("#live-line");

const phrases = [
  "Мне нужно быстро сформулировать мысль…",
  "Запиши эту мысль для документа…",
  "Собери из этого понятную задачу для ИИ.",
];

if (liveLine && !window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
  let index = 0;
  window.setInterval(() => {
    index = (index + 1) % phrases.length;
    liveLine.animate(
      [
        { opacity: 1, transform: "translateY(0)" },
        { opacity: 0, transform: "translateY(7px)", offset: 0.45 },
        { opacity: 0, transform: "translateY(-7px)", offset: 0.55 },
        { opacity: 1, transform: "translateY(0)" },
      ],
      { duration: 520, easing: "ease" }
    );
    window.setTimeout(() => {
      liveLine.textContent = phrases[index];
    }, 270);
  }, 3200);
}
