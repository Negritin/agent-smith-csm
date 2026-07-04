'use client';

import React, { useState } from 'react';
import { motion, AnimatePresence, Variants } from 'framer-motion';
import Image from 'next/image';
import { Lightning } from './Lightning';

interface FeatureItemProps {
  name: string;
  value: string;
  position: string;
}

const FeatureItem: React.FC<FeatureItemProps> = ({ name, value, position }) => {
  return (
    <div className={`absolute ${position} z-10 group transition-all duration-300 hover:scale-110`}>
      <div className="flex items-center gap-2 relative">
        <div className="relative">
          <div className="w-2 h-2 bg-white rounded-full group-hover:animate-pulse"></div>
          <div className="absolute -inset-1 bg-white/20 rounded-full blur-sm opacity-70 group-hover:opacity-100 transition-opacity duration-300"></div>
        </div>
        <div className="text-white relative">
          <div className="font-medium group-hover:text-white transition-colors duration-300">
            {name}
          </div>
          <div className="text-white/70 text-sm group-hover:text-white/70 transition-colors duration-300">
            {value}
          </div>
          <div className="absolute -inset-2 bg-white/10 rounded-lg blur-md opacity-70 group-hover:opacity-100 transition-opacity duration-300 -z-10"></div>
        </div>
      </div>
    </div>
  );
};

export const HeroSection: React.FC = () => {
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const [lightningHue] = useState(220);

  const containerVariants: Variants = {
    hidden: { opacity: 0 },
    visible: {
      opacity: 1,
      transition: {
        staggerChildren: 0.3,
        delayChildren: 0.2,
      },
    },
  };

  const itemVariants: Variants = {
    hidden: { y: 20, opacity: 0 },
    visible: {
      y: 0,
      opacity: 1,
      transition: {
        duration: 0.5,
      },
    },
  };

  return (
    <div className="landing-legacy-theme relative w-full bg-[#020817] text-white overflow-hidden">
      <div className="relative z-20 max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6 h-screen">
        <motion.div
          initial={{ y: -20, opacity: 0 }}
          animate={{ y: 0, opacity: 1 }}
          transition={{ duration: 0.5 }}
          className="px-4 backdrop-blur-3xl bg-[#020817]/50 border border-[#263047]/50 rounded-full py-4 flex justify-between items-center mb-12"
        >
          <div className="flex items-center">
            <div className="text-2xl font-bold flex items-center justify-center w-10 h-10">
              <Image
                src="/smith-logo.png"
                alt="Smith Logo"
                width={40}
                height={40}
                className="w-10 h-10"
              />
            </div>
            <div className="hidden md:flex items-center space-x-6 ml-8">
              <button
                onClick={() => (window.location.href = '/landing')}
                className="px-4 py-2 text-sm hover:text-muted-foreground transition-colors"
              >
                Home
              </button>
            </div>
          </div>
          <div className="flex items-center space-x-4">
            <button
              onClick={() => (window.location.href = '/login')}
              className="hidden md:block px-4 py-2 text-sm hover:text-muted-foreground transition-colors"
            >
              Login
            </button>
            <button
              onClick={() => (window.location.href = '/admin')}
              className="px-4 py-2 bg-[#1f2937]/80 backdrop-blur-sm rounded-full text-sm hover:bg-[#1f2937] transition-colors text-white"
            >
              Admin
            </button>
            <button
              className="md:hidden p-2 rounded-md focus:outline-none"
              onClick={() => setMobileMenuOpen(!mobileMenuOpen)}
            >
              {mobileMenuOpen ? (
                <svg className="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeWidth={2}
                    d="M6 18L18 6M6 6l12 12"
                  />
                </svg>
              ) : (
                <svg className="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeWidth={2}
                    d="M4 6h16M4 12h16M4 18h16"
                  />
                </svg>
              )}
            </button>
          </div>
        </motion.div>

        {mobileMenuOpen && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="md:hidden fixed inset-0 z-50 bg-[#020817]/95 backdrop-blur-lg"
          >
            <div className="flex flex-col items-center justify-center h-full space-y-6 text-lg">
              <button
                className="absolute top-6 right-6 p-2"
                onClick={() => setMobileMenuOpen(false)}
              >
                <svg className="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeWidth={2}
                    d="M6 18L18 6M6 6l12 12"
                  />
                </svg>
              </button>
              <button onClick={() => (window.location.href = '/landing')} className="px-6 py-3">
                Home
              </button>
              <button onClick={() => (window.location.href = '/login')} className="px-6 py-3">
                Login
              </button>
              <button
                onClick={() => (window.location.href = '/admin')}
                className="px-6 py-3 bg-[#1f2937]/80 backdrop-blur-sm rounded-full"
              >
                Admin
              </button>
            </div>
          </motion.div>
        )}

        <motion.div
          variants={containerVariants}
          initial="hidden"
          animate="visible"
          className="w-full z-200 top-[30%] relative"
        >
          <motion.div variants={itemVariants}>
            <FeatureItem
              name="Seus Dados"
              value="para privacidade"
              position="left-0 sm:left-10 top-40"
            />
          </motion.div>
          <motion.div variants={itemVariants}>
            <FeatureItem
              name="Sua Infraestrutura"
              value="para controle"
              position="left-1/4 top-24"
            />
          </motion.div>
          <motion.div variants={itemVariants}>
            <FeatureItem name="Compliance Total" value="para LGPD" position="right-1/4 top-24" />
          </motion.div>
          <motion.div variants={itemVariants}>
            <FeatureItem
              name="Multi-formato"
              value="para documentos"
              position="right-0 sm:right-10 top-40"
            />
          </motion.div>
        </motion.div>

        <motion.div
          variants={containerVariants}
          initial="hidden"
          animate="visible"
          className="relative z-30 flex flex-col items-center text-center max-w-4xl mx-auto"
        >
          <motion.button
            variants={itemVariants}
            whileHover={{ scale: 1.05 }}
            whileTap={{ scale: 0.95 }}
            className="flex items-center space-x-2 px-4 py-2 bg-[#263047]/20 hover:bg-[#263047]/30 backdrop-blur-sm rounded-full text-sm mb-6 transition-all duration-300 group border border-white/10"
          >
            <span>Agent Smith v7.0</span>
            <svg
              width="16"
              height="16"
              viewBox="0 0 16 16"
              fill="none"
              className="transform group-hover:translate-x-1 transition-transform duration-300"
            >
              <path
                d="M8 3L13 8L8 13M13 8H3"
                stroke="currentColor"
                strokeWidth="1.5"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </motion.button>

          <motion.h1 variants={itemVariants} className="text-5xl md:text-7xl font-light mb-2">
            Smith AI
          </motion.h1>

          <motion.h2
            variants={itemVariants}
            className="text-3xl md:text-5xl pb-3 font-light bg-gradient-to-r from-white via-white/80 to-white/50 bg-clip-text text-transparent"
          >
            Seu Assistente Inteligente
          </motion.h2>

          <motion.p variants={itemVariants} className="text-[#94a3b8] mb-9 max-w-2xl">
            Converse com Smith, seu assistente pessoal com inteligência artificial avançada.
          </motion.p>

          <motion.button
            variants={itemVariants}
            whileHover={{ scale: 1.05 }}
            whileTap={{ scale: 0.95 }}
            onClick={() => (window.location.href = '/login')}
            className="mt-[100px] sm:mt-[100px] px-8 py-3 bg-[#263047]/20 backdrop-blur-sm rounded-full hover:bg-[#263047]/30 transition-colors border border-white/10"
          >
            Começar Agora
          </motion.button>
        </motion.div>
      </div>

      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ duration: 1 }}
        className="absolute inset-0 z-0"
      >
        <div className="absolute inset-0 bg-[#020817]/80"></div>
        <div className="absolute top-[55%] left-1/2 transform -translate-x-1/2 -translate-y-1/2 w-[800px] h-[800px] rounded-full bg-gradient-to-b from-blue-500/20 to-cyan-500/10 blur-3xl"></div>
        <div className="absolute top-0 w-[100%] left-1/2 transform -translate-x-1/2 h-full">
          <Lightning hue={lightningHue} xOffset={0} speed={1.6} intensity={0.6} size={2} />
        </div>
        <div className="z-10 absolute top-[55%] left-1/2 transform -translate-x-1/2 w-[600px] h-[600px] backdrop-blur-3xl rounded-full bg-[radial-gradient(circle_at_25%_90%,_#1e386b_15%,_#000000de_70%,_#000000ed_100%)]"></div>
      </motion.div>
    </div>
  );
};
