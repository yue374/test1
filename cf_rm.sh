#!/bin/bash
# Colored output logs
green_log() {
    echo -e "\e[32m$1\e[0m"
}
red_log() {
    echo -e "\e[31m$1\e[0m"
}
yellow_log() {
    echo -e "\e[33m$1\e[0m"
}
