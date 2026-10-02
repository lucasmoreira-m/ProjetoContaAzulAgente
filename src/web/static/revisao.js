// Telas de revisão: soma do rateio ao vivo (só aviso, não bloqueia) e
// bloqueio de clique duplo ao enviar um formulário.
(function () {
  const numero = (t) => {
    t = String(t || "").replace(/R\$|\s/g, "");
    if (t.includes(",")) t = t.replace(/\./g, "").replace(",", ".");
    const n = parseFloat(t);
    return isNaN(n) ? 0 : n;
  };
  const brl = (n) => n.toLocaleString("pt-BR", { style: "currency", currency: "BRL" });

  document.querySelectorAll("form").forEach((form) => {
    const campoValor = form.querySelector('input[name="valor"]');
    const campoLiquido = form.querySelector('input[name="valor_liquido"]');
    const saidaRateio = form.querySelector(".soma-rateio");
    const saidaLiquido = form.querySelector(".confere-liquido");

    const atualizar = () => {
      const total = numero(campoValor && campoValor.value);
      if (saidaRateio) {
        let soma = 0;
        form.querySelectorAll(".tabela-rateio tbody tr").forEach((tr) => {
          const v = numero(tr.querySelector(".rateio-valor").value);
          if (tr.querySelector(".rateio-incluir").checked && v > 0) soma += v;
        });
        const dif = Math.round((soma - total) * 100) / 100;
        saidaRateio.textContent = Math.abs(dif) > 0.01
          ? "⚠️ Soma do rateio (" + brl(soma) + ") difere do valor total (" + brl(total) + ") em " + brl(dif) + "."
          : "✅ Soma do rateio confere com o valor total: " + brl(soma);
        saidaRateio.classList.toggle("alerta", Math.abs(dif) > 0.01);
      }
      if (saidaLiquido && campoLiquido) {
        const imposto = numero(saidaLiquido.dataset.imposto);
        const liquido = numero(campoLiquido.value);
        const confere = Math.abs(liquido + imposto - total) <= 0.01;
        saidaLiquido.textContent = confere
          ? "✅ Líquido + imposto confere com o valor do serviço."
          : "⚠️ Líquido (" + brl(liquido) + ") + imposto (" + brl(imposto) + ") = " + brl(liquido + imposto) +
            ", não bate com o valor do serviço (" + brl(total) + "). Confira antes de aprovar.";
        saidaLiquido.classList.toggle("alerta", !confere);
      }
    };
    form.addEventListener("input", atualizar);
    atualizar();

    form.addEventListener("submit", (e) => {
      if (e.defaultPrevented) return; // ex.: "Cancelar" no confirm do Descartar
      // Depois do envio (o botão clicado já foi incluído nos dados).
      setTimeout(() => form.querySelectorAll("button").forEach((b) => (b.disabled = true)), 0);
    });
  });
})();
