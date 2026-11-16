cmake_minimum_required(VERSION 3.22)

#
# This file is generated only once,
# and is not re-generated if converter is called multiple times.
#
# User is free to modify the file as much as necessary
#

# Setup compiler settings
set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_C_EXTENSIONS ON)


# Define the build type
if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE "Debug")
endif()

# Set the project name
set(CMAKE_PROJECT_NAME G0B1_PWM_Practice)

# Enable compile command to ease indexing with e.g. clangd
set(CMAKE_EXPORT_COMPILE_COMMANDS TRUE)

# Core project settings
project(${CMAKE_PROJECT_NAME})
message("Build type: " ${CMAKE_BUILD_TYPE})

# Enable CMake support for ASM and C languages
enable_language(C ASM)

# Create an executable object type
add_executable(${CMAKE_PROJECT_NAME})

# Add STM32CubeMX generated sources
add_subdirectory(cmake/stm32cubemx)

# Link directories setup
target_link_directories(${CMAKE_PROJECT_NAME} PRIVATE
    # Add user defined library search paths
)

# Add sources to executable
target_sources(${CMAKE_PROJECT_NAME} PRIVATE
    # Add user sources here
)

# Add include paths
target_include_directories(${CMAKE_PROJECT_NAME} PRIVATE
    # Add user defined include paths
)

# Add project symbols (macros)
target_compile_definitions(${CMAKE_PROJECT_NAME} PRIVATE
    # Add user defined symbols
)

# Remove wrong libob.a library dependency when using cpp files
list(REMOVE_ITEM CMAKE_C_IMPLICIT_LINK_LIBRARIES ob)

# Add linked libraries
target_link_libraries(${CMAKE_PROJECT_NAME}
    stm32cubemx

    # Add user defined libraries
)

# Legacy variants retain their original behavior; 0.4.0 onward enables states.
# Never overwrite published 0.1/0.2/0.3 artifacts: approval uses immutable hashes.
# Outputs stay in the build tree; publishing releases is a separate action.
get_target_property(APP_SOURCES ${CMAKE_PROJECT_NAME} SOURCES)
foreach(VERSION IN ITEMS 0.1.0 0.2.0 0.3.0 0.4.0 0.5.0)
    string(REPLACE "." "_" VERSION_TARGET "${VERSION}")
    set(TARGET "firmware_${VERSION_TARGET}")
    add_executable(${TARGET} ${APP_SOURCES})
    target_link_libraries(${TARGET} PRIVATE stm32cubemx STM32_Drivers ${TOOLCHAIN_LINK_LIBRARIES})
    target_compile_definitions(${TARGET} PRIVATE APP_VERSION="${VERSION}")
    if(VERSION VERSION_GREATER_EQUAL "0.4.0")
        target_compile_definitions(${TARGET} PRIVATE APP_STATE_TELEMETRY=1)
    endif()
    set_target_properties(${TARGET} PROPERTIES
        RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/versions/${VERSION}"
        OUTPUT_NAME firmware)
    target_link_options(${TARGET} PRIVATE "-Wl,-Map=${CMAKE_BINARY_DIR}/versions/${VERSION}/firmware.map")
    add_custom_command(TARGET ${TARGET} POST_BUILD
        COMMAND ${CMAKE_OBJCOPY} -O binary $<TARGET_FILE:${TARGET}>
            "${CMAKE_BINARY_DIR}/versions/${VERSION}/firmware.bin"
        VERBATIM)
endforeach()
