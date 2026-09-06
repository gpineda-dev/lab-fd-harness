#!/usr/bin/env bash
# ==============================================================================
# Sample 09: Interactive CLI Calculator App
# Demonstrates human user input on dedicated FD 4 proxied by Python supervisor,
# Pratt arithmetic, standard math functions (sin, cos, sqrt, exp),
# and Casio/TI-style variable assignment: <expr> => <var>.
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../harness/lib_harness.sh"

MEMORY="0"

print_header() {
    echo "============================================================"
    echo "       PRATT COPROCESSOR CALCULATOR (Powered by fd-harness) "
    echo "============================================================"
    echo " Features & Expressions:"
    echo "   <expr>            Evaluate (e.g. 5 * 6, 1s / 60, sqrt(64))"
    echo "   <expr> => <var>   Store into variable (e.g. 8 + 6 => var1)"
    echo "   Math Functions    sin, cos, tan, sqrt, exp, log, abs, round"
    echo "   Math Constants    pi (3.14159...), e (2.71828...), tau"
    echo ""
    echo " Slash Commands:"
    echo "   /list, /vars      List registered variables in coprocessor memory"
    echo "   /mem              Display accumulator ($MEMORY)"
    echo "   /clear            Reset accumulator to 0"
    echo "   /sprint <text>    Interpolate with [bracketed expressions]"
    echo "   /help             Display this menu"
    echo "   /exit, /quit      Exit calculator"
    echo "============================================================"
}

print_header

while true; do
    echo -n "calc [mem=$MEMORY]> "
    
    # Read user input from dedicated FD 4 proxied by Python supervisor
    if ! harness_user_read line; then
        echo ""
        echo "[SH] End of input detected. Exiting."
        break
    fi

    # Trim leading/trailing whitespace
    cmd=$(echo "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
    [[ -z "$cmd" ]] && continue

    case "$cmd" in
        /quit|/exit|/q|quit|exit|q)
            echo "Goodbye!"
            break
            ;;
        /help|/h|help|h)
            print_header
            ;;
        /list|/vars|vars|list)
            harness_calc_list
            if [[ "$HARNESS_COUNT" -eq 0 || "$HARNESS_VARS" == "none" ]]; then
                echo "No variables registered."
            else
                echo "Registered variables (${HARNESS_COUNT}):"
                IFS=',' read -ra VAR_ENTRIES <<< "$HARNESS_VARS"
                for entry in "${VAR_ENTRIES[@]}"; do
                    var_k="${entry%%=*}"
                    var_v="${entry#*=}"
                    echo "  ${var_k} = ${var_v}"
                done
            fi
            ;;
        /mem|mem)
            echo "Accumulator MEM = $MEMORY"
            ;;
        /clear|clear)
            MEMORY="0"
            echo "Memory cleared: MEM = 0"
            ;;
        /sprint\ *|sprint\ *)
            if [[ "$cmd" == /sprint* ]]; then
                template="${cmd#/sprint }"
            else
                template="${cmd#sprint }"
            fi
            harness_sprint "$template"
            echo "Rendered: $HARNESS_VAL"
            ;;
        *)
            # Check for Casio/TI-style variable assignment: <expr> => <var> or <expr> -> <var>
            store_var=""
            raw_expr="$cmd"
            if [[ "$cmd" == *"=>"* ]]; then
                raw_expr="${cmd%%=>*}"
                store_var=$(echo "${cmd##*=>}" | tr -d '[:space:]')
            elif [[ "$cmd" == *"->"* ]]; then
                raw_expr="${cmd%%->*}"
                store_var=$(echo "${cmd##*->}" | tr -d '[:space:]')
            fi

            # Substitute 'mem' keyword in expression if present
            expr="${raw_expr//mem/$MEMORY}"
            
            # Delegate evaluation to Pratt Coprocessor (with optional store)
            harness_calc "$expr" "$store_var"
            
            if [[ "$HARNESS_VAL" == error:* ]]; then
                echo "Syntax Error: $HARNESS_VAL"
            else
                if [[ -n "$store_var" ]]; then
                    echo "Assigned: $HARNESS_VAL"
                    # Update accumulator to value
                    MEMORY="${HARNESS_VAL#*=}"
                else
                    MEMORY="$HARNESS_VAL"
                    echo "Result: $MEMORY"
                fi
            fi
            ;;
    esac
done
